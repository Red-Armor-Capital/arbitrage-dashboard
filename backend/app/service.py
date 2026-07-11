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
from .models import DashboardResponse, DashboardSummary, Instrument, VenueStatus
from .storage import CarryStore


logger = logging.getLogger(__name__)
HISTORY_REFRESH_INTERVAL = timedelta(hours=1)
HISTORY_FAILURE_RETRY_INTERVAL = timedelta(minutes=2)
HistoryKey = tuple[str, str]


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
        self.adapters = [factory(self.client, config.underlyings) for factory in adapter_factories]
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._history_refreshed_at: dict[str, datetime] = {}
        self._history_next_attempt_at: dict[HistoryKey, datetime] = {}
        self._history_failures: dict[HistoryKey, str] = {}
        self._refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._refresh_loop(), name="carry-refresh-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()

    async def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("refresh loop failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.refresh_seconds
                )
            except TimeoutError:
                continue

    async def refresh_once(self) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            await asyncio.gather(
                *(self._refresh_adapter(adapter) for adapter in self.adapters),
                return_exceptions=True,
            )

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
            history_error: str | None = None
            if uses_per_symbol_history:
                try:
                    history_error = await self._refresh_per_symbol_history(
                        adapter,
                        result.instruments,
                        history_since,
                        now,
                    )
                except Exception as exc:
                    logger.warning("%s history refresh failed: %s", adapter.venue, exc)
                    error_text = f"{type(exc).__name__}: {exc}".rstrip()
                    history_error = f"Partial history: batch failed ({error_text[:200]})"
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
            self.store.upsert_status(
                VenueStatus(
                    venue=adapter.venue,
                    status="offline",
                    last_error=error_text[:500],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
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
                self.store.upsert_funding(batch.funding)
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
        since = datetime.now(timezone.utc) - timedelta(days=self.config.history_lookback_days)
        settled = self.store.get_settled_funding(since)
        opportunities = build_carry_opportunities(
            current_rows=current,
            settled_rows=settled,
            lookback_days=self.config.history_lookback_days,
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
