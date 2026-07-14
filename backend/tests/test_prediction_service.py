from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.adapters.base import VenueAdapter
from backend.app.config import Settings
from backend.app.models import AdapterResult, Instrument, MarketSnapshot
from backend.app.service import CarryService
from backend.app.storage import CarryStore


UTC = timezone.utc


def test_repository_default_disables_prediction_collection() -> None:
    assert Settings(_env_file=None).prediction_collection_enabled is False


class PredictionAdapter(VenueAdapter):
    venue = "lighter"
    observed_at = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(
        minutes=1
    )

    async def collect(
        self, history_since: datetime, include_history: bool = True
    ) -> AdapterResult:
        target = self.observed_at.replace(minute=0, second=0, microsecond=0) + timedelta(
            hours=1
        )
        return AdapterResult(
            instruments=[
                Instrument(
                    venue=self.venue,
                    symbol="NVDA-USD",
                    underlying="NVDA",
                    funding_interval_hours=1,
                )
            ],
            snapshots=[
                MarketSnapshot(
                    venue=self.venue,
                    symbol="NVDA-USD",
                    underlying="NVDA",
                    observed_at=self.observed_at,
                    funding_rate=0.001,
                    funding_interval_hours=1,
                    next_funding_at=target,
                    raw_funding_rate=0.1,
                    raw_rate_unit="percent",
                    source_tenor_hours=1,
                    target_source="schedule",
                    transform_version="test-v1",
                )
            ],
        )


class FailingPredictionAdapter(PredictionAdapter):
    async def collect(
        self, history_since: datetime, include_history: bool = True
    ) -> AdapterResult:
        raise RuntimeError("venue unavailable")


class SlowHistoryPredictionAdapter(PredictionAdapter):
    history_mode = "per_symbol"

    def __init__(self, client, underlyings) -> None:
        super().__init__(client, underlyings)
        self.history_started = asyncio.Event()
        self.history_release = asyncio.Event()

    async def _history(self, instrument, since):
        self.history_started.set()
        await self.history_release.wait()
        return []


class SecondSlowHistoryPredictionAdapter(SlowHistoryPredictionAdapter):
    venue = "extended"


class SlowGeneralAdapter(VenueAdapter):
    venue = "slow-general"

    def __init__(self, client, underlyings) -> None:
        super().__init__(client, underlyings)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def collect(
        self, history_since: datetime, include_history: bool = True
    ) -> AdapterResult:
        self.started.set()
        await self.release.wait()
        return AdapterResult()


@pytest.mark.asyncio
async def test_service_collects_and_exposes_prediction_status(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="lighter",
            prediction_archive_dir=tmp_path / "archive",
            prediction_collection_enabled=True,
        ),
        store,
        [PredictionAdapter],
    )
    try:
        await service.refresh_once()
        await service.refresh_once()
        status = service.prediction_collector_status()
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM funding_prediction_minutes"
            ).fetchone()[0]
    finally:
        await service.stop()

    assert rows == 1
    assert status.enabled is True
    assert status.poll_seconds == 30
    assert status.sample_resolution_seconds == 60
    assert status.hot_retention_days == 90
    assert status.hot_rows == 1
    assert status.venues[0].venue == "lighter"
    assert status.venues[0].last_flushed_minute == (
        PredictionAdapter.observed_at.replace(second=0, microsecond=0)
    )


@pytest.mark.asyncio
async def test_service_records_prediction_failure_separately(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="lighter",
            prediction_collection_enabled=True,
        ),
        store,
        [FailingPredictionAdapter],
    )
    try:
        await service.refresh_once()
        prediction_status = service.prediction_collector_status().venues[0]
        venue_status = store.get_statuses()[0]
    finally:
        await service.stop()

    assert prediction_status.status == "offline"
    assert "venue unavailable" in (prediction_status.last_error or "")
    assert venue_status["status"] == "offline"


@pytest.mark.asyncio
async def test_prediction_collection_can_be_disabled(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="lighter",
            prediction_collection_enabled=False,
        ),
        store,
        [PredictionAdapter],
    )
    try:
        await service.refresh_once()
        status = service.prediction_collector_status()
    finally:
        await service.stop()

    assert status.enabled is False
    assert status.hot_rows == 0
    assert status.venues == []


@pytest.mark.asyncio
async def test_dex_history_backfill_does_not_block_live_refresh(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="lighter",
        ),
        store,
        [SlowHistoryPredictionAdapter],
    )
    adapter = service.adapters[0]
    assert isinstance(adapter, SlowHistoryPredictionAdapter)
    try:
        await asyncio.wait_for(service._refresh_adapter(adapter), timeout=0.5)
        await asyncio.wait_for(adapter.history_started.wait(), timeout=0.5)
        task = service._background_history_tasks["lighter"]
        assert task.done() is False
        assert store.get_statuses()[0]["status"] == "healthy"

        adapter.history_release.set()
        await asyncio.wait_for(task, timeout=0.5)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_dex_history_backfill_respects_global_venue_concurrency(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="lighter,extended",
            history_venue_concurrency=1,
        ),
        store,
        [SlowHistoryPredictionAdapter, SecondSlowHistoryPredictionAdapter],
    )
    first, second = service.adapters
    assert isinstance(first, SlowHistoryPredictionAdapter)
    assert isinstance(second, SecondSlowHistoryPredictionAdapter)
    now = datetime.now(UTC)
    history_since = now - timedelta(days=7)

    service._schedule_background_history(
        first,
        [Instrument(venue=first.venue, symbol="NVDA-USD", underlying="NVDA")],
        history_since,
        now,
    )
    try:
        await asyncio.wait_for(first.history_started.wait(), timeout=0.5)
        service._schedule_background_history(
            second,
            [Instrument(venue=second.venue, symbol="NVDA-USD", underlying="NVDA")],
            history_since,
            now,
        )
        await asyncio.sleep(0)
        assert second.history_started.is_set() is False

        first.history_release.set()
        await asyncio.wait_for(second.history_started.wait(), timeout=0.5)
        second.history_release.set()
        await asyncio.wait_for(
            asyncio.gather(*service._background_history_tasks.values()),
            timeout=0.5,
        )
    finally:
        first.history_release.set()
        second.history_release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_slow_general_venues_do_not_block_prediction_refresh(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="slow-general,lighter",
            prediction_collection_enabled=True,
        ),
        store,
        [SlowGeneralAdapter, PredictionAdapter],
    )
    slow = service.adapters[0]
    assert isinstance(slow, SlowGeneralAdapter)
    general_task = asyncio.create_task(
        service._refresh_group(prediction_group=False)
    )
    try:
        await asyncio.wait_for(slow.started.wait(), timeout=0.5)
        await asyncio.wait_for(
            service._refresh_group(prediction_group=True), timeout=0.5
        )
        assert [row["venue"] for row in store.get_current_rows()] == ["lighter"]
        assert general_task.done() is False

        slow.release.set()
        await asyncio.wait_for(general_task, timeout=0.5)
    finally:
        slow.release.set()
        await service.stop()
