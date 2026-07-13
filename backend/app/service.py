from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone

import httpx

from .adapters.base import VenueAdapter
from .analytics import MIN_STABILITY_SAMPLE_HOURS, build_carry_opportunities
from .config import Settings
from .models import (
    DashboardResponse,
    DashboardSummary,
    Instrument,
    PredictionCollectorStatusResponse,
    VenueStatus,
)
from .prediction import MinutePredictionCollector
from .storage import CarryStore
from .us_equity import US_EQUITY_VENUE, collect_us_equity_quotes, get_us_spot_spec


logger = logging.getLogger(__name__)
HISTORY_REFRESH_INTERVAL = timedelta(hours=1)
HISTORY_FAILURE_RETRY_INTERVAL = timedelta(minutes=2)
HISTORY_WRITE_BATCH_SIZE = 1_000
HistoryKey = tuple[str, str]
PREDICTION_VENUES = frozenset({"lighter", "extended", "xyz", "hotstuff", "orderly"})
ARCHIVE_MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60
PREDICTION_FLUSH_INTERVAL_SECONDS = 1


class CarryService:
    def __init__(
        self,
        config: Settings,
        store: CarryStore,
        adapter_factories: list[type[VenueAdapter]],
    ) -> None:
        self.config = config
        self.store = store
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_seconds),
            headers={"User-Agent": "equity-carry-monitor/0.1"},
        )
        self.adapters = [
            factory(self.client, config.underlyings) for factory in adapter_factories
        ]
        self._task: asyncio.Task | None = None
        self._prediction_refresh_task: asyncio.Task | None = None
        self._prediction_flush_task: asyncio.Task | None = None
        self._prediction_maintenance_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._history_refreshed_at: dict[str, datetime] = {}
        self._history_next_attempt_at: dict[HistoryKey, datetime] = {}
        self._history_failures: dict[HistoryKey, str] = {}
        self._background_history_tasks: dict[str, asyncio.Task] = {}
        self._general_refresh_lock = asyncio.Lock()
        self._prediction_refresh_lock = asyncio.Lock()
        self.prediction_collector = (
            MinutePredictionCollector(
                store,
                status_window_minutes=config.prediction_status_window_minutes,
            )
            if config.prediction_collection_enabled
            else None
        )
        self._last_archive_checked_at: datetime | None = None
        self._last_archive_error: str | None = None
        self._last_us_equity_refresh_at: datetime | None = None
        self._us_equity_offset = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._refresh_loop(), name="carry-refresh-loop")
        if self._prediction_refresh_task is None:
            self._prediction_refresh_task = asyncio.create_task(
                self._prediction_refresh_loop(),
                name="prediction-live-refresh-loop",
            )
        if (
            self.prediction_collector is not None
            and self._prediction_maintenance_task is None
        ):
            self._prediction_flush_task = asyncio.create_task(
                self._prediction_flush_loop(),
                name="prediction-minute-flush",
            )
            self._prediction_maintenance_task = asyncio.create_task(
                self._prediction_maintenance_loop(),
                name="prediction-archive-maintenance",
            )

    async def stop(self) -> None:
        self._stop_event.set()
        tasks = [
            task
            for task in (
                self._task,
                self._prediction_refresh_task,
                self._prediction_flush_task,
                self._prediction_maintenance_task,
                *self._background_history_tasks.values(),
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()

    async def _refresh_loop(self) -> None:
        await self._run_refresh_loop(prediction_group=False)

    async def _prediction_refresh_loop(self) -> None:
        await self._run_refresh_loop(prediction_group=True)

    async def _run_refresh_loop(self, *, prediction_group: bool) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                await self._refresh_group(prediction_group=prediction_group)
            except Exception:
                group = "prediction" if prediction_group else "general"
                logger.exception("%s refresh loop failed", group)
            remaining = max(
                0.0,
                self.config.refresh_seconds - (time.monotonic() - started),
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=remaining
                )
            except TimeoutError:
                continue

    async def _prediction_flush_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.prediction_collector is not None:
                try:
                    self.prediction_collector.flush_completed(
                        datetime.now(timezone.utc)
                    )
                except Exception:
                    logger.exception("prediction minute flush failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=PREDICTION_FLUSH_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

    async def refresh_once(self) -> None:
        await asyncio.gather(
            self._refresh_group(prediction_group=False),
            self._refresh_group(prediction_group=True),
        )
        if self.prediction_collector is not None:
            try:
                self.prediction_collector.flush_completed(datetime.now(timezone.utc))
            except Exception:
                logger.exception("prediction minute flush failed")

    async def _refresh_group(self, *, prediction_group: bool) -> None:
        lock = (
            self._prediction_refresh_lock
            if prediction_group
            else self._general_refresh_lock
        )
        if lock.locked():
            return
        adapters = [
            adapter
            for adapter in self.adapters
            if (adapter.venue in PREDICTION_VENUES) == prediction_group
        ]
        async with lock:
            await asyncio.gather(
                *(self._refresh_adapter(adapter) for adapter in adapters),
                return_exceptions=True,
            )
            if prediction_group:
                await self._refresh_us_equity_quotes()

    async def _refresh_us_equity_quotes(self) -> None:
        if US_EQUITY_VENUE not in self.config.venues:
            return
        now = datetime.now(timezone.utc)
        if (
            self._last_us_equity_refresh_at is not None
            and (now - self._last_us_equity_refresh_at).total_seconds()
            < self.config.us_equity_refresh_seconds
        ):
            return

        available_underlyings = {
            str(row["underlying"]).upper()
            for row in self.store.get_current_rows()
            if row.get("venue") in PREDICTION_VENUES
            and get_us_spot_spec(str(row["underlying"])) is not None
        }
        if not available_underlyings:
            return

        priority = [
            underlying
            for underlying in ("BB", "SKHYNIX")
            if underlying in available_underlyings
        ]
        rotating = sorted(available_underlyings - set(priority))
        rotating_slots = max(0, self.config.us_equity_batch_size - len(priority))
        if rotating and rotating_slots:
            start = self._us_equity_offset % len(rotating)
            selected = [
                rotating[(start + offset) % len(rotating)]
                for offset in range(min(rotating_slots, len(rotating)))
            ]
            self._us_equity_offset = (start + len(selected)) % len(rotating)
        else:
            selected = []
        underlyings = set(priority + selected)

        started = time.perf_counter()
        try:
            collection = await collect_us_equity_quotes(self.client, underlyings)
            result = collection.result
            # Quotes are refreshed in rotating batches. Upsert the current batch
            # without deactivating still-valid last-good quotes from other batches.
            self.store.upsert_instruments(result.instruments)
            self.store.upsert_snapshots(result.snapshots)
            self._last_us_equity_refresh_at = datetime.now(timezone.utc)
            errors = list(collection.errors)
            if not result.snapshots:
                errors.insert(0, "No live US equity quotes returned")
            self.store.upsert_status(
                VenueStatus(
                    venue=US_EQUITY_VENUE,
                    status=(
                        "healthy"
                        if not errors
                        else "degraded" if result.snapshots else "offline"
                    ),
                    last_success_at=(
                        datetime.now(timezone.utc) if result.snapshots else None
                    ),
                    last_error="; ".join(errors)[:500] if errors else None,
                    instruments=len(result.snapshots),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )
        except Exception as exc:
            logger.warning("US equity quote refresh failed: %s", exc)
            self.store.upsert_status(
                VenueStatus(
                    venue=US_EQUITY_VENUE,
                    status="offline",
                    last_error=f"{type(exc).__name__}: {exc}"[:500],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )

    async def _prediction_maintenance_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                archive_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.store.archive_old_prediction_months,
                        datetime.now(timezone.utc),
                        self.config.prediction_hot_days,
                        self.config.prediction_archive_dir,
                    )
                )
                try:
                    await asyncio.shield(archive_task)
                except asyncio.CancelledError:
                    await archive_task
                    raise
                self._last_archive_checked_at = datetime.now(timezone.utc)
                self._last_archive_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_archive_checked_at = datetime.now(timezone.utc)
                self._last_archive_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception("prediction archive maintenance failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=ARCHIVE_MAINTENANCE_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

    async def _refresh_adapter(self, adapter: VenueAdapter) -> None:
        if adapter.venue not in self.config.venues:
            return
        started = time.perf_counter()
        now = datetime.now(timezone.utc)
        uses_per_symbol_history = adapter.history_mode == "per_symbol"
        last_history_refresh = self._history_refreshed_at.get(adapter.venue)
        include_history = not uses_per_symbol_history and (
            last_history_refresh is None
            or now - last_history_refresh >= HISTORY_REFRESH_INTERVAL
        )
        history_since = now - timedelta(days=self.config.history_lookback_days)
        try:
            result = await adapter.collect(history_since, include_history=include_history)
            self.store.sync_instruments(adapter.venue, result.instruments)
            self.store.upsert_snapshots(result.snapshots)
            self.store.upsert_funding(result.funding)
            if (
                self.prediction_collector is not None
                and adapter.venue in PREDICTION_VENUES
            ):
                try:
                    self.prediction_collector.ingest(
                        adapter.venue,
                        result.snapshots,
                        expected_symbols=len(result.instruments),
                    )
                except Exception as exc:
                    logger.warning(
                        "%s prediction collection failed: %s", adapter.venue, exc
                    )
                    self._record_prediction_failure(
                        adapter.venue,
                        f"{type(exc).__name__}: {exc}"[:500],
                        expected_symbols=len(result.instruments),
                    )
            history_error: str | None = None
            if uses_per_symbol_history:
                if adapter.venue in PREDICTION_VENUES:
                    history_error = self._history_failure_summary(
                        adapter,
                        result.instruments,
                    )
                    self._schedule_background_history(
                        adapter,
                        result.instruments,
                        history_since,
                        now,
                    )
                else:
                    try:
                        history_error = await self._refresh_per_symbol_history(
                            adapter,
                            result.instruments,
                            history_since,
                            now,
                        )
                    except Exception as exc:
                        logger.warning(
                            "%s history refresh failed: %s", adapter.venue, exc
                        )
                        error_text = f"{type(exc).__name__}: {exc}".rstrip()
                        history_error = (
                            f"Partial history: batch failed ({error_text[:200]})"
                        )
            elif include_history:
                self._history_refreshed_at[adapter.venue] = datetime.now(timezone.utc)

            live_error = (
                None
                if result.snapshots
                else "No live stock-perpetual snapshots returned"
            )
            errors = [error for error in (live_error, history_error) if error]
            self.store.upsert_status(
                VenueStatus(
                    venue=adapter.venue,
                    status="healthy" if not errors else "degraded",
                    last_success_at=datetime.now(timezone.utc),
                    instruments=len(result.instruments),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    last_error="; ".join(errors)[:500] if errors else None,
                )
            )
        except Exception as exc:
            logger.warning("%s refresh failed: %s", adapter.venue, exc)
            error_text = f"{type(exc).__name__}: {exc}".rstrip()
            if (
                self.prediction_collector is not None
                and adapter.venue in PREDICTION_VENUES
            ):
                self._record_prediction_failure(
                    adapter.venue,
                    error_text[:500],
                )
            self.store.upsert_status(
                VenueStatus(
                    venue=adapter.venue,
                    status="offline",
                    last_error=error_text[:500],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )

    def _record_prediction_failure(
        self,
        venue: str,
        error: str,
        expected_symbols: int = 0,
    ) -> None:
        if self.prediction_collector is None:
            return
        try:
            self.prediction_collector.note_failure(
                venue,
                error,
                expected_symbols=expected_symbols,
            )
        except Exception:
            logger.exception("%s prediction status update failed", venue)

    def _schedule_background_history(
        self,
        adapter: VenueAdapter,
        instruments: list[Instrument],
        history_since: datetime,
        now: datetime,
    ) -> None:
        existing = self._background_history_tasks.get(adapter.venue)
        if existing is not None and not existing.done():
            return
        self._background_history_tasks[adapter.venue] = asyncio.create_task(
            self._run_background_history(
                adapter,
                list(instruments),
                history_since,
                now,
            ),
            name=f"{adapter.venue}-funding-history",
        )

    async def _run_background_history(
        self,
        adapter: VenueAdapter,
        instruments: list[Instrument],
        history_since: datetime,
        now: datetime,
    ) -> None:
        try:
            await self._refresh_per_symbol_history(
                adapter,
                instruments,
                history_since,
                now,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s background history refresh failed", adapter.venue)

    def prediction_collector_status(self) -> PredictionCollectorStatusResponse:
        stored = self.store.get_prediction_collector_status()
        return PredictionCollectorStatusResponse(
            generated_at=datetime.now(timezone.utc),
            enabled=self.prediction_collector is not None,
            poll_seconds=self.config.refresh_seconds,
            sample_resolution_seconds=60,
            status_window_minutes=self.config.prediction_status_window_minutes,
            hot_retention_days=self.config.prediction_hot_days,
            archive_dir=str(self.config.prediction_archive_dir),
            last_archive_checked_at=self._last_archive_checked_at,
            last_archive_error=self._last_archive_error,
            **stored,
        )

    async def _refresh_per_symbol_history(
        self,
        adapter: VenueAdapter,
        instruments: list[Instrument],
        history_since: datetime,
        now: datetime,
    ) -> str | None:
        selected = {
            instrument.symbol: instrument
            for instrument in adapter.history_instruments(instruments)
        }
        active_keys = {(adapter.venue, symbol) for symbol in selected}

        tracked_keys = set(self._history_next_attempt_at) | set(self._history_failures)
        for key in [
            key
            for key in tracked_keys
            if key[0] == adapter.venue and key not in active_keys
        ]:
            self._history_next_attempt_at.pop(key, None)
            self._history_failures.pop(key, None)

        due = [
            instrument
            for symbol, instrument in selected.items()
            if now >= self._history_next_attempt_at.get(
                (adapter.venue, symbol),
                datetime.min.replace(tzinfo=timezone.utc),
            )
        ]
        if due:
            batch_error: str | None = None
            try:
                batch = await adapter.collect_history(due, history_since)
                for offset in range(0, len(batch.funding), HISTORY_WRITE_BATCH_SIZE):
                    self.store.upsert_funding(
                        batch.funding[offset : offset + HISTORY_WRITE_BATCH_SIZE]
                    )
                    await asyncio.sleep(0)
            except Exception as exc:
                batch_error = f"{type(exc).__name__}: {exc}".rstrip()[:240]
                batch = None
            completed_at = datetime.now(timezone.utc)
            if batch_error is not None:
                for instrument in due:
                    key = (adapter.venue, instrument.symbol)
                    self._history_next_attempt_at[key] = (
                        completed_at + HISTORY_FAILURE_RETRY_INTERVAL
                    )
                    self._history_failures[key] = batch_error

            seen: set[HistoryKey] = set()
            for outcome in batch.outcomes if batch is not None else []:
                key = (adapter.venue, outcome.instrument.symbol)
                if key not in active_keys:
                    continue
                seen.add(key)
                if outcome.success:
                    self._history_next_attempt_at[key] = (
                        completed_at + HISTORY_REFRESH_INTERVAL
                    )
                    self._history_failures.pop(key, None)
                else:
                    self._history_next_attempt_at[key] = (
                        completed_at + HISTORY_FAILURE_RETRY_INTERVAL
                    )
                    self._history_failures[key] = outcome.error or "unknown error"

            for instrument in due:
                key = (adapter.venue, instrument.symbol)
                if batch is None or key in seen:
                    continue
                self._history_next_attempt_at[key] = (
                    completed_at + HISTORY_FAILURE_RETRY_INTERVAL
                )
                self._history_failures[key] = "no outcome returned"

        return self._history_failure_summary(adapter, instruments)

    def _history_failure_summary(
        self,
        adapter: VenueAdapter,
        instruments: list[Instrument],
    ) -> str | None:
        active_keys = {
            (adapter.venue, instrument.symbol)
            for instrument in adapter.history_instruments(instruments)
        }
        failures = sorted(
            (
                symbol,
                error,
            )
            for (venue, symbol), error in self._history_failures.items()
            if venue == adapter.venue and (venue, symbol) in active_keys
        )
        if not failures:
            return None
        preview = ", ".join(
            f"{symbol}: {error}" for symbol, error in failures[:3]
        )
        if len(failures) > 3:
            preview += f", +{len(failures) - 3} more"
        return f"Partial history: {len(failures)} symbol(s) failed ({preview})"[:500]

    def dashboard(self) -> DashboardResponse:
        current = self.store.get_current_rows()
        spot_rows = self.store.get_spot_rows()
        since = datetime.now(timezone.utc) - timedelta(days=self.config.history_lookback_days)
        settled = self.store.get_settled_funding(since)
        opportunities = build_carry_opportunities(
            current_rows=current,
            settled_rows=settled,
            lookback_days=self.config.history_lookback_days,
            spot_rows=spot_rows,
        )
        statuses = [VenueStatus.model_validate(value) for value in self.store.get_statuses()]
        sufficiently_sampled = [
            item
            for item in opportunities
            if item.sample_hours >= MIN_STABILITY_SAMPLE_HOURS
            and item.mean_carry_apr is not None
        ]
        breakevens = [
            item.breakeven_hours
            for item in sufficiently_sampled
            if item.breakeven_hours is not None
        ]
        summary = DashboardSummary(
            opportunities=len(opportunities),
            best_carry_apr=(
                max(item.mean_carry_apr for item in sufficiently_sampled)
                if sufficiently_sampled
                else None
            ),
            median_breakeven_hours=statistics.median(breakevens) if breakevens else None,
            stable_opportunities=sum(
                item.history_quality == "sufficient"
                and item.positive_ratio is not None
                and item.mean_carry_apr is not None
                and item.carry_apr_volatility is not None
                and item.positive_ratio >= 0.8
                and item.carry_apr_volatility <= max(abs(item.mean_carry_apr), 1.0)
                for item in opportunities
            ),
            venues_healthy=sum(item.status == "healthy" for item in statuses),
            venues_total=len(statuses),
        )
        return DashboardResponse(
            lookback_days=self.config.history_lookback_days,
            summary=summary,
            opportunities=opportunities,
            venues=statuses,
        )
