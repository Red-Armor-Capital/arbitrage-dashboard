import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.app.adapters.base import VenueAdapter
from backend.app.config import Settings
from backend.app.models import (
    AdapterResult,
    FundingRate,
    Instrument,
    MarketSnapshot,
    VenueStatus,
)
from backend.app.service import CarryService, _fresh_live_rows, _rows_from_usable_venues
from backend.app.storage import CarryStore
from backend.app.security_registry import get_security


class FakeAdapter(VenueAdapter):
    venue = "test"
    history_flags: list[bool] = []

    async def collect(
        self, history_since: datetime, include_history: bool = True
    ) -> AdapterResult:
        self.history_flags.append(include_history)
        return AdapterResult(
            instruments=[
                Instrument(venue=self.venue, symbol="NVDA-USD", underlying="NVDA")
            ]
        )


class RecordingStore(CarryStore):
    def __init__(self, path) -> None:
        self.sync_calls: list[tuple[str, list[str]]] = []
        super().__init__(path)

    def sync_instruments(self, venue: str, items: list[Instrument]) -> None:
        self.sync_calls.append((venue, [item.symbol for item in items]))
        super().sync_instruments(venue, items)


class PerSymbolAdapter(VenueAdapter):
    venue = "per-symbol"
    history_mode = "per_symbol"

    def __init__(self, client: httpx.AsyncClient, underlyings: set[str]) -> None:
        super().__init__(client, underlyings)
        self.symbols = ["A", "B"]
        self.failed_symbols: set[str] = set()
        self.empty_symbols: set[str] = set()
        self.history_attempts: list[str] = []
        self.live_history_flags: list[bool] = []

    def _instrument(self, symbol: str) -> Instrument:
        return Instrument(venue=self.venue, symbol=symbol, underlying=symbol)

    async def collect(
        self, history_since: datetime, include_history: bool = True
    ) -> AdapterResult:
        self.live_history_flags.append(include_history)
        instruments = [self._instrument(symbol) for symbol in self.symbols]
        snapshots = [
            MarketSnapshot(
                venue=self.venue,
                symbol=symbol,
                underlying=symbol,
                mark_price=100,
                funding_rate=0.001,
                funding_interval_hours=1,
            )
            for symbol in self.symbols
        ]
        return AdapterResult(instruments=instruments, snapshots=snapshots)

    async def _history(
        self, instrument: Instrument, since: datetime
    ) -> list[FundingRate]:
        self.history_attempts.append(instrument.symbol)
        if instrument.symbol in self.failed_symbols:
            raise RuntimeError(f"{instrument.symbol} unavailable")
        if instrument.symbol in self.empty_symbols:
            return []
        return [
            FundingRate(
                venue=self.venue,
                symbol=instrument.symbol,
                underlying=instrument.underlying,
                effective_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                rate=0.001,
                interval_hours=1,
                kind="settled",
            )
        ]


class ConcurrencyAdapter(PerSymbolAdapter):
    history_concurrency = 2

    def __init__(self, client: httpx.AsyncClient, underlyings: set[str]) -> None:
        super().__init__(client, underlyings)
        self.active_history_requests = 0
        self.max_active_history_requests = 0

    async def _history(
        self, instrument: Instrument, since: datetime
    ) -> list[FundingRate]:
        self.active_history_requests += 1
        self.max_active_history_requests = max(
            self.max_active_history_requests,
            self.active_history_requests,
        )
        try:
            await asyncio.sleep(0.01)
            return await super()._history(instrument, since)
        finally:
            self.active_history_requests -= 1


class HighConcurrencyAdapter(ConcurrencyAdapter):
    history_concurrency = 8


@pytest.mark.asyncio
async def test_service_caps_history_concurrency_per_adapter_instance(tmp_path) -> None:
    service = CarryService(
        Settings(
            _env_file=None,
            database_path=tmp_path / "carry.duckdb",
            history_symbol_concurrency=2,
        ),
        CarryStore(tmp_path / "store.duckdb"),
        [HighConcurrencyAdapter],
    )

    try:
        adapter = service.adapters[0]
        adapter.symbols = ["A", "B", "C", "D"]
        instruments = [adapter._instrument(symbol) for symbol in adapter.symbols]
        await adapter.collect_history(
            instruments,
            datetime.now(timezone.utc) - timedelta(days=7),
        )

        assert adapter.max_active_history_requests == 2
        assert HighConcurrencyAdapter.history_concurrency == 8
    finally:
        await service.stop()


def test_dashboard_row_filters_fail_closed_for_offline_and_stale_sources() -> None:
    now = datetime.now(timezone.utc)
    statuses = [
        VenueStatus(
            venue="healthy",
            status="healthy",
            last_success_at=now,
        ),
        VenueStatus(
            venue="degraded",
            status="degraded",
            last_success_at=now,
        ),
        VenueStatus(
            venue="offline",
            status="offline",
            last_success_at=now - timedelta(minutes=5),
        ),
    ]
    rows = [
        {"venue": "healthy", "symbol": "fresh", "observed_at": now},
        {
            "venue": "degraded",
            "symbol": "partial-but-fresh",
            "observed_at": now - timedelta(seconds=30),
        },
        {
            "venue": "healthy",
            "symbol": "stale",
            "observed_at": now - timedelta(seconds=121),
        },
        {
            "venue": "healthy",
            "symbol": "cached-upstream",
            "observed_at": now,
            "source_observed_at": now - timedelta(seconds=121),
        },
        {"venue": "offline", "symbol": "cached", "observed_at": now},
        {"venue": "missing", "symbol": "unknown", "observed_at": now},
    ]

    assert [
        row["symbol"]
        for row in _fresh_live_rows(
            rows,
            statuses,
            now=now,
            max_age_seconds=120,
        )
    ] == ["fresh", "partial-but-fresh"]
    assert [
        row["symbol"] for row in _rows_from_usable_venues(rows, statuses)
    ] == ["fresh", "partial-but-fresh", "stale", "cached-upstream"]


def test_service_discovers_spot_securities_by_exact_contract_identity(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    now = datetime.now(timezone.utc)
    store.upsert_instruments(
        [
            Instrument(
                venue="xyz",
                symbol="xyz:MINIMAX",
                underlying="MINIMAX",
                metadata={"asset_class": "stock", "spot_carry_eligible": True},
            ),
            Instrument(
                venue="xyz",
                symbol="xyz:SPCX",
                underlying="SPACEX",
                metadata={"asset_class": "preipo", "spot_carry_eligible": False},
            ),
        ]
    )
    store.upsert_snapshots(
        [
            MarketSnapshot(
                venue="xyz",
                symbol=symbol,
                underlying=underlying,
                observed_at=now,
                funding_rate=0.0001,
                funding_interval_hours=1,
            )
            for symbol, underlying in (
                ("xyz:MINIMAX", "MINIMAX"),
                ("xyz:SPCX", "SPACEX"),
            )
        ]
    )
    service = CarryService(
        Settings(database_path=tmp_path / "unused.duckdb", enabled_venues="xyz"),
        store,
        [],
    )

    try:
        securities = service._mapped_spot_securities()
    finally:
        asyncio.run(service.stop())

    assert [(item.security_id, item.ticker) for item in securities] == [
        ("HK:XHKG:0100", "0100.HK")
    ]


def test_security_annotation_invalidates_a_failed_rotating_quote() -> None:
    security = get_security("US:XNYS:BB")
    assert security is not None
    result = AdapterResult(
        instruments=[
            Instrument(
                venue="us_equity",
                symbol="BB",
                underlying="BB",
                product_type="stock",
                metadata={"quote_valid": True},
            )
        ]
    )

    CarryService._attach_security_identity(result, [security])

    assert result.instruments[0].metadata["security_id"] == "US:XNYS:BB"
    assert result.instruments[0].metadata["quote_valid"] is False


@pytest.mark.asyncio
async def test_service_syncs_catalog_and_periodically_refreshes_history(tmp_path) -> None:
    FakeAdapter.history_flags = []
    store = RecordingStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(database_path=tmp_path / "unused.duckdb", enabled_venues="test"),
        store,
        [FakeAdapter],
    )

    try:
        adapter = service.adapters[0]
        await service._refresh_adapter(adapter)
        await service._refresh_adapter(adapter)
        service._history_refreshed_at[adapter.venue] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        await service._refresh_adapter(adapter)
    finally:
        await service.stop()

    assert FakeAdapter.history_flags == [True, False, True]
    assert store.sync_calls == [
        ("test", ["NVDA-USD"]),
        ("test", ["NVDA-USD"]),
        ("test", ["NVDA-USD"]),
    ]


@pytest.mark.asyncio
async def test_per_symbol_history_retries_only_failed_symbol(tmp_path) -> None:
    store = RecordingStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="per-symbol",
        ),
        store,
        [PerSymbolAdapter],
    )
    adapter = service.adapters[0]
    assert isinstance(adapter, PerSymbolAdapter)
    adapter.failed_symbols = {"B"}

    try:
        await service._refresh_adapter(adapter)

        assert adapter.live_history_flags == [False]
        assert Counter(adapter.history_attempts) == Counter({"A": 1, "B": 1})
        assert {
            row["symbol"]
            for row in store.get_settled_funding(
                datetime.now(timezone.utc) - timedelta(days=1)
            )
        } == {"A"}
        status = store.get_statuses()[0]
        assert status["status"] == "degraded"
        assert "Partial history" in status["last_error"]
        assert "B" in status["last_error"]
        assert service._history_next_attempt_at[(adapter.venue, "B")] > (
            datetime.now(timezone.utc) + timedelta(minutes=1, seconds=50)
        )
        assert service._history_next_attempt_at[(adapter.venue, "A")] > (
            datetime.now(timezone.utc) + timedelta(minutes=50)
        )

        adapter.failed_symbols.clear()
        service._history_next_attempt_at[(adapter.venue, "B")] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        await service._refresh_adapter(adapter)

        assert Counter(adapter.history_attempts) == Counter({"A": 1, "B": 2})
        status = store.get_statuses()[0]
        assert status["status"] == "healthy"
        assert status["last_error"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_per_symbol_empty_history_is_success_and_waits_an_hour(tmp_path) -> None:
    store = RecordingStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="per-symbol",
        ),
        store,
        [PerSymbolAdapter],
    )
    adapter = service.adapters[0]
    assert isinstance(adapter, PerSymbolAdapter)
    adapter.symbols = ["A"]
    adapter.empty_symbols = {"A"}

    try:
        await service._refresh_adapter(adapter)
        await service._refresh_adapter(adapter)

        assert adapter.history_attempts == ["A"]
        assert service._history_failures == {}
        assert service._history_next_attempt_at[(adapter.venue, "A")] > (
            datetime.now(timezone.utc) + timedelta(minutes=50)
        )
        status = store.get_statuses()[0]
        assert status["status"] == "healthy"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_per_symbol_history_expires_hourly_and_discovers_new_symbols(tmp_path) -> None:
    store = RecordingStore(tmp_path / "carry.duckdb")
    service = CarryService(
        Settings(
            database_path=tmp_path / "unused.duckdb",
            enabled_venues="per-symbol",
        ),
        store,
        [PerSymbolAdapter],
    )
    adapter = service.adapters[0]
    assert isinstance(adapter, PerSymbolAdapter)
    adapter.symbols = ["A"]

    try:
        await service._refresh_adapter(adapter)
        await service._refresh_adapter(adapter)
        assert adapter.history_attempts == ["A"]

        adapter.symbols.append("B")
        await service._refresh_adapter(adapter)
        assert Counter(adapter.history_attempts) == Counter({"A": 1, "B": 1})

        service._history_next_attempt_at[(adapter.venue, "A")] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        await service._refresh_adapter(adapter)
        assert Counter(adapter.history_attempts) == Counter({"A": 2, "B": 1})

        adapter.symbols = ["B"]
        await service._refresh_adapter(adapter)
        assert (adapter.venue, "A") not in service._history_next_attempt_at
        assert (adapter.venue, "A") not in service._history_failures
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_collect_history_bounds_concurrency_and_reports_empty_success() -> None:
    client = httpx.AsyncClient()
    adapter = ConcurrencyAdapter(client, set())
    adapter.symbols = ["A", "B", "C", "D"]
    adapter.empty_symbols = {"B"}
    adapter.failed_symbols = {"C"}
    instruments = [adapter._instrument(symbol) for symbol in adapter.symbols]

    try:
        batch = await adapter.collect_history(
            instruments,
            datetime.now(timezone.utc) - timedelta(days=7),
        )
    finally:
        await client.aclose()

    outcomes = {outcome.instrument.symbol: outcome for outcome in batch.outcomes}
    assert adapter.max_active_history_requests == 2
    assert outcomes["A"].success is True
    assert outcomes["B"].success is True
    assert outcomes["B"].funding == []
    assert outcomes["C"].success is False
    assert "C unavailable" in (outcomes["C"].error or "")
    assert outcomes["D"].success is True
    assert {rate.symbol for rate in batch.funding} == {"A", "D"}
