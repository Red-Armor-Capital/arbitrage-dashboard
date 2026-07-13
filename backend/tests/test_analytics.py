from datetime import datetime, timedelta, timezone

import pytest

from backend.app.analytics import HOURS_PER_YEAR, build_carry_opportunities


def _current(venue: str, symbol: str, rate: float, fee: float) -> dict:
    return {
        "venue": venue,
        "symbol": symbol,
        "underlying": "NVDA",
        "observed_at": datetime.now(timezone.utc),
        "bid": 100.0,
        "ask": 100.1,
        "mark_price": 100.05,
        "index_price": 100.0,
        "funding_rate": rate,
        "funding_interval_hours": 1.0,
        "next_funding_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "open_interest": 1_000.0,
        "volume_24h": 1_000_000.0,
        "display_name": "NVIDIA",
        "asset_class": "stock",
        "spot_carry_eligible": True,
        "maker_fee": 0.0,
        "taker_fee": fee,
    }


def _spot(symbol: str, price: float, observed_at: datetime | None = None) -> dict:
    return {
        "venue": "us_equity",
        "symbol": symbol,
        "underlying": symbol,
        "observed_at": observed_at or datetime.now(timezone.utc),
        "bid": None,
        "ask": None,
        "mark_price": price,
        "index_price": price,
        "display_name": symbol,
        "quote_source": "test quote",
        "provider_symbol": symbol,
        "quote_session": "regular",
        "quote_delayed": False,
    }


def test_builds_short_high_long_low_and_breakeven() -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    current = [
        _current("high", "NVDA-HIGH", 0.00002, 0.00025),
        _current("low", "NVDA-LOW", 0.000005, 0.0001),
    ]
    settled = []
    for offset in range(1, 25):
        settled.extend(
            [
                {
                    "venue": "high",
                    "symbol": "NVDA-HIGH",
                    "underlying": "NVDA",
                    "effective_at": now - timedelta(hours=offset - 1),
                    "rate": 0.00002,
                    "interval_hours": 1.0,
                },
                {
                    "venue": "low",
                    "symbol": "NVDA-LOW",
                    "underlying": "NVDA",
                    "effective_at": now - timedelta(hours=offset - 1),
                    "rate": 0.000005,
                    "interval_hours": 1.0,
                },
            ]
        )

    rows = build_carry_opportunities(current, settled, lookback_days=7)

    perp_rows = [row for row in rows if row.strategy_type == "perp_perp"]
    assert len(perp_rows) == 1
    row = perp_rows[0]
    assert row.price_assumption == "observed"
    assert row.fee_scope == "both_legs"
    assert row.short_venue == "high"
    assert row.long_venue == "low"
    assert row.mean_carry_apr == pytest.approx(0.000015 * HOURS_PER_YEAR * 100)
    assert row.carry_apr_volatility == pytest.approx(0)
    assert row.positive_ratio == pytest.approx(1)
    expected_fee = 2 * (0.00025 + 0.0001)
    assert row.round_trip_fee_pct == pytest.approx(expected_fee * 100)
    assert row.breakeven_hours == pytest.approx(expected_fee / 0.000015)


def test_uses_current_rate_when_no_history_exists() -> None:
    rows = build_carry_opportunities(
        [
            _current("high", "NVDA-HIGH", 0.00002, 0.00025),
            _current("low", "NVDA-LOW", 0.000005, 0.0001),
        ],
        [],
        lookback_days=7,
    )
    perp_rows = [row for row in rows if row.strategy_type == "perp_perp"]
    assert len(perp_rows) == 1
    assert perp_rows[0].sample_hours == 0
    assert perp_rows[0].history_quality == "unavailable"
    assert perp_rows[0].mean_carry_apr is None
    assert perp_rows[0].carry_apr_volatility is None
    assert perp_rows[0].positive_ratio is None
    assert perp_rows[0].breakeven_hours is None
    assert perp_rows[0].indicative_breakeven_hours is not None


def test_rejects_large_cross_venue_price_mismatch() -> None:
    high = _current("high", "NVDA-HIGH", 0.00002, 0.00025)
    low = _current("low", "NVDA-LOW", 0.000005, 0.0001)
    low["mark_price"] = 50.0
    low["index_price"] = 50.0

    rows = build_carry_opportunities([high, low], [], lookback_days=7)

    assert [row for row in rows if row.strategy_type == "perp_perp"] == []


def test_builds_us_spot_chain_perp_from_single_perp_history() -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    current = [_current("xyz", "xyz:NVDA", 0.00002, 0.00009)]
    settled = [
        {
            "venue": "xyz",
            "symbol": "xyz:NVDA",
            "underlying": "NVDA",
            "effective_at": now - timedelta(hours=offset - 1),
            "rate": 0.00002,
            "interval_hours": 1.0,
        }
        for offset in range(1, 25)
    ]

    rows = build_carry_opportunities(
        current, settled, lookback_days=7, spot_rows=[_spot("NVDA", 100.0)]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.strategy_type == "spot_perp"
    assert row.price_assumption == "us_spot_quote"
    assert row.fee_scope == "perp_leg_only"
    assert row.long_venue == "us_equity"
    assert row.long_symbol == "NVDA"
    assert row.short_venue == "xyz"
    assert row.current_carry_apr == pytest.approx(0.00002 * HOURS_PER_YEAR * 100)
    assert row.mean_carry_apr == pytest.approx(0.00002 * HOURS_PER_YEAR * 100)
    assert row.long_funding_apr == 0
    assert row.short_funding_apr == pytest.approx(0.00002 * HOURS_PER_YEAR * 100)
    assert row.spot_price_usd == 100
    assert row.spot_equivalent_price_usd == 100
    assert row.perp_price_usd == pytest.approx(100.05)
    assert row.spot_perp_basis_pct == pytest.approx(0.05)
    assert row.sample_hours == 24
    assert row.history_quality == "sufficient"
    assert row.positive_ratio == 1
    assert row.round_trip_fee_pct == pytest.approx(2 * 0.00009 * 100)
    assert row.breakeven_hours == pytest.approx((2 * 0.00009) / 0.00002)


@pytest.mark.parametrize(
    ("venue", "rate"),
    [("binance", 0.00002), ("xyz", 0.0), ("xyz", -0.00001)],
)
def test_does_not_build_synthetic_spot_for_cex_or_non_positive_funding(
    venue: str,
    rate: float,
) -> None:
    rows = build_carry_opportunities(
        [_current(venue, f"{venue}:NVDA", rate, 0.0001)], [], lookback_days=7
    )

    assert rows == []


@pytest.mark.parametrize(
    ("asset_class", "eligible"),
    [
        ("index", False),
        ("preipo", False),
        ("basket", False),
        ("unknown", False),
        ("stock", False),
    ],
)
def test_synthetic_spot_requires_explicit_stock_or_etf_eligibility(
    asset_class: str,
    eligible: bool,
) -> None:
    row = _current("xyz", "xyz:NVDA", 0.00002, 0.00009)
    row["asset_class"] = asset_class
    row["spot_carry_eligible"] = eligible

    assert build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[_spot("NVDA", 100)]
    ) == []


def test_synthetic_spot_accepts_an_explicitly_eligible_etf() -> None:
    row = _current("xyz", "xyz:SPY", 0.00002, 0.00009)
    row["underlying"] = "SPY"
    row["asset_class"] = "etf"

    opportunities = build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[_spot("SPY", 100)]
    )

    assert len(opportunities) == 1
    assert opportunities[0].asset_class == "etf"


def test_skhynix_uses_ten_ads_and_keeps_large_real_basis() -> None:
    row = _current("lighter", "SKHYNIXUSD", 0.00002, 0.00009)
    row.update(
        underlying="SKHYNIX",
        display_name="SK Hynix",
        mark_price=1280.0,
        index_price=1279.0,
    )

    opportunities = build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[_spot("SKHY", 160.0)]
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.spot_symbol == "SKHY"
    assert opportunity.spot_units_per_perp_unit == 10
    assert opportunity.spot_equivalent_price_usd == 1600
    assert opportunity.perp_price_usd == 1280
    assert opportunity.spot_perp_basis_pct == pytest.approx(-20)
    assert "quanto" in (opportunity.price_comparison_note or "")


def test_bb_uses_one_to_one_us_share_price() -> None:
    row = _current("xyz", "xyz:BB", 0.00002, 0.00009)
    row.update(underlying="BB", mark_price=11.5, index_price=11.4)

    opportunities = build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[_spot("BB", 10.0)]
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.spot_units_per_perp_unit == 1
    assert opportunity.spot_equivalent_price_usd == 10
    assert opportunity.spot_perp_basis_pct == pytest.approx(15)


def test_spot_perp_requires_an_observed_us_spot_quote() -> None:
    row = _current("xyz", "xyz:BB", 0.00002, 0.00009)
    row["underlying"] = "BB"

    assert build_carry_opportunities([row], [], lookback_days=7) == []
