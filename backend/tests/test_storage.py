from datetime import datetime, timezone

import pytest

from backend.app.models import FundingRate, Instrument, MarketSnapshot, VenueStatus
from backend.app.storage import CarryStore


def test_store_round_trip(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    now = datetime.now(timezone.utc)
    instrument = Instrument(
        venue="test",
        symbol="NVDA-USD",
        underlying="NVDA",
        maker_fee=0.0001,
        taker_fee=0.0002,
        funding_interval_hours=1,
        metadata={"asset_class": "stock", "spot_carry_eligible": True},
    )
    store.upsert_instruments([instrument])
    store.upsert_snapshots(
        [
            MarketSnapshot(
                venue="test",
                symbol="NVDA-USD",
                underlying="STALE_SNAPSHOT_NAME",
                observed_at=now,
                mark_price=100,
                index_price=99.9,
                funding_rate=0.0001,
                funding_interval_hours=1,
            )
        ]
    )
    store.upsert_funding(
        [
            FundingRate(
                venue="test",
                symbol="NVDA-USD",
                underlying="STALE_FUNDING_NAME",
                observed_at=now,
                effective_at=now,
                rate=0.0001,
                interval_hours=1,
                kind="settled",
            )
        ]
    )
    store.upsert_status(
        VenueStatus(venue="test", status="healthy", last_success_at=now, instruments=1)
    )

    current = store.get_current_rows()[0]
    assert current["underlying"] == "NVDA"
    assert current["funding_rate"] == 0.0001
    assert current["asset_class"] == "stock"
    assert current["spot_carry_eligible"] is True

    settled = store.get_settled_funding(now.replace(hour=0, minute=0, second=0))[0]
    assert settled["underlying"] == "NVDA"
    assert settled["rate"] == 0.0001
    assert store.get_statuses()[0]["status"] == "healthy"


def test_stock_quote_is_separate_from_perpetual_rows(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    now = datetime.now(timezone.utc)
    store.upsert_instruments(
        [
            Instrument(
                venue="us_equity",
                symbol="BB",
                underlying="BB",
                product_type="stock",
                metadata={
                    "quote_source": "Nasdaq delayed",
                    "provider_symbol": "BB",
                    "quote_session": "closed",
                    "quote_delayed": True,
                },
            )
        ]
    )
    store.upsert_snapshots(
        [
            MarketSnapshot(
                venue="us_equity",
                symbol="BB",
                underlying="BB",
                observed_at=now,
                mark_price=10.97,
                index_price=10.97,
            )
        ]
    )

    assert store.get_current_rows() == []
    assert store.get_spot_rows() == [
        {
            "venue": "us_equity",
            "symbol": "BB",
            "underlying": "BB",
            "observed_at": now,
            "bid": None,
            "ask": None,
            "mark_price": 10.97,
            "index_price": 10.97,
            "display_name": None,
            "quote_source": "Nasdaq delayed",
            "provider_symbol": "BB",
            "quote_session": "closed",
            "quote_delayed": True,
        }
    ]


def test_instrument_sync_deactivates_only_missing_symbols(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    now = datetime.now(timezone.utc)
    original = [
        Instrument(venue="test", symbol="AAPL-USD", underlying="AAPL"),
        Instrument(venue="test", symbol="NVDA-USD", underlying="NVDA"),
    ]
    store.sync_instruments("test", original)
    store.upsert_snapshots(
        [
            MarketSnapshot(
                venue="test",
                symbol=item.symbol,
                underlying=item.underlying,
                observed_at=now,
                funding_rate=0.0001,
                funding_interval_hours=1,
            )
            for item in original
        ]
    )
    store.upsert_funding(
        [
            FundingRate(
                venue="test",
                symbol=item.symbol,
                underlying=item.underlying,
                observed_at=now,
                effective_at=now,
                rate=0.0001,
                interval_hours=1,
                kind="settled",
            )
            for item in original
        ]
    )

    store.sync_instruments(
        "test",
        [
            Instrument(
                venue="test",
                symbol="NVDA-USD",
                underlying="NVDA_CANONICAL",
                metadata={"asset_class": "not-a-real-class", "spot_carry_eligible": "yes"},
            )
        ],
    )

    assert store.get_current_rows() == [
        {
            "venue": "test",
            "symbol": "NVDA-USD",
            "underlying": "NVDA_CANONICAL",
            "observed_at": now,
            "bid": None,
            "ask": None,
            "mark_price": None,
            "index_price": None,
            "funding_rate": 0.0001,
            "funding_interval_hours": 1.0,
            "next_funding_at": None,
            "open_interest": None,
            "volume_24h": None,
            "display_name": None,
            "maker_fee": 0.0,
            "taker_fee": 0.0,
            "asset_class": "unknown",
            "spot_carry_eligible": False,
        }
    ]
    settled = store.get_settled_funding(now.replace(hour=0, minute=0, second=0))
    assert [(row["symbol"], row["underlying"]) for row in settled] == [
        ("NVDA-USD", "NVDA_CANONICAL")
    ]


def test_instrument_sync_rejects_mixed_venue_without_disabling_catalog(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    now = datetime.now(timezone.utc)
    store.sync_instruments(
        "test",
        [Instrument(venue="test", symbol="NVDA-USD", underlying="NVDA")],
    )
    store.upsert_snapshots(
        [
            MarketSnapshot(
                venue="test",
                symbol="NVDA-USD",
                underlying="NVDA",
                observed_at=now,
                funding_rate=0.0001,
            )
        ]
    )

    with pytest.raises(ValueError, match="synchronized venue"):
        store.sync_instruments(
            "test",
            [Instrument(venue="other", symbol="AAPL-USD", underlying="AAPL")],
        )

    assert [row["symbol"] for row in store.get_current_rows()] == ["NVDA-USD"]
