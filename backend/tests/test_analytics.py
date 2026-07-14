from datetime import datetime, timedelta, timezone

import pytest

from backend.app.analytics import HOURS_PER_YEAR, build_carry_opportunities
from backend.app.security_registry import SECURITIES


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


def _spot(
    symbol: str,
    price: float,
    observed_at: datetime | None = None,
    *,
    venue: str = "us_equity",
    underlying: str | None = None,
    market: str = "US",
    local_price: float | None = None,
    currency: str = "USD",
    local_per_usd: float = 1.0,
) -> dict:
    security = next(
        (
            spec
            for spec in SECURITIES
            if spec.spot_venue == venue and spec.ticker.upper() == symbol.upper()
        ),
        None,
    )
    return {
        "venue": venue,
        "symbol": symbol,
        "underlying": underlying or symbol,
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
        "quote_valid": security is not None,
        "spot_market": market,
        "security_id": security.security_id if security else None,
        "mic": security.mic if security else None,
        "local_price": local_price if local_price is not None else price,
        "local_currency": currency,
        "local_per_usd": local_per_usd,
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
    assert row.long_liquidity is not None
    assert row.long_liquidity.volume_24h_usd == pytest.approx(1_000_000)
    assert row.long_liquidity.open_interest_usd == pytest.approx(100_050)
    assert row.short_liquidity.volume_24h_usd == pytest.approx(1_000_000)
    assert row.short_liquidity.open_interest_usd == pytest.approx(100_050)


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
    assert row.price_assumption == "spot_quote"
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
    assert row.long_liquidity is None
    assert row.short_liquidity.venue == "xyz"
    assert row.short_liquidity.volume_24h_usd == pytest.approx(1_000_000)
    assert row.short_liquidity.open_interest_usd == pytest.approx(100_050)
    assert row.sample_hours == 24
    assert row.history_quality == "sufficient"
    assert row.positive_ratio == 1
    assert row.round_trip_fee_pct == pytest.approx(2 * 0.00009 * 100)
    assert row.breakeven_hours == pytest.approx((2 * 0.00009) / 0.00002)


def test_does_not_build_spot_perp_for_cex() -> None:
    rows = build_carry_opportunities(
        [_current("binance", "NVDA-USDT", 0.00002, 0.0001)],
        [],
        lookback_days=7,
        spot_rows=[_spot("NVDA", 100.0)],
    )

    assert rows == []


def test_spot_perp_keeps_negative_current_carry_when_settled_mean_is_positive() -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    current_rate = -0.00001
    settled_rate = 0.00002
    fee = 0.0001
    settled = [
        {
            "venue": "xyz",
            "symbol": "xyz:NVDA",
            "underlying": "NVDA",
            "effective_at": now - timedelta(hours=offset - 1),
            "rate": settled_rate,
            "interval_hours": 1.0,
        }
        for offset in range(1, 25)
    ]

    rows = build_carry_opportunities(
        [_current("xyz", "xyz:NVDA", current_rate, fee)],
        settled,
        lookback_days=7,
        spot_rows=[_spot("NVDA", 100.0)],
    )

    assert len(rows) == 1
    opportunity = rows[0]
    assert opportunity.strategy_type == "spot_perp"
    assert opportunity.current_carry_apr == pytest.approx(
        current_rate * HOURS_PER_YEAR * 100
    )
    assert opportunity.mean_carry_apr == pytest.approx(
        settled_rate * HOURS_PER_YEAR * 100
    )
    assert opportunity.indicative_breakeven_hours is None
    assert opportunity.breakeven_hours == pytest.approx((2 * fee) / settled_rate)


def test_spot_perp_keeps_negative_current_carry_without_history() -> None:
    rows = build_carry_opportunities(
        [_current("xyz", "xyz:NVDA", -0.00001, 0.0001)],
        [],
        lookback_days=7,
        spot_rows=[_spot("NVDA", 100.0)],
    )

    assert len(rows) == 1
    opportunity = rows[0]
    assert opportunity.current_carry_apr < 0
    assert opportunity.mean_carry_apr is None
    assert opportunity.history_quality == "unavailable"
    assert opportunity.breakeven_hours is None
    assert opportunity.indicative_breakeven_hours is None


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
    row = _current("orderly", "PERP_SPY_USDC_mythos", 0.00002, 0.00009)
    row["underlying"] = "SPY"
    row["asset_class"] = "etf"

    opportunities = build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[_spot("SPY", 100)]
    )

    assert len(opportunities) == 1
    assert opportunities[0].asset_class == "etf"


def test_skhynix_uses_korean_common_share_and_krw_conversion() -> None:
    korean_perp = _current("lighter", "SKHYNIXUSD", 0.00002, 0.00009)
    korean_perp.update(
        underlying="SKHYNIX",
        display_name="SK Hynix",
        mark_price=1280.0,
        index_price=1279.0,
    )

    opportunities = build_carry_opportunities(
        [korean_perp],
        [],
        lookback_days=7,
        spot_rows=[
            _spot(
                "000660.KS",
                1_919_000 / 1495.8,
                venue="kr_equity",
                underlying="SKHYNIX",
                market="KR",
                local_price=1_919_000,
                currency="KRW",
                local_per_usd=1495.8,
            ),
            _spot("SKHY", 160.0, underlying="SKHY"),
        ],
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.long_venue == "kr_equity"
    assert opportunity.spot_market == "KR"
    assert opportunity.spot_symbol == "000660.KS"
    assert opportunity.spot_units_per_perp_unit == 1
    assert opportunity.spot_price_local == 1_919_000
    assert opportunity.spot_currency == "KRW"
    assert opportunity.spot_local_per_usd == pytest.approx(1495.8)
    assert opportunity.spot_equivalent_price_usd == pytest.approx(1_919_000 / 1495.8)
    assert opportunity.perp_price_usd == 1280
    assert opportunity.spot_perp_basis_pct == pytest.approx(
        (1280 / (1_919_000 / 1495.8) - 1) * 100
    )
    assert "quanto" in (opportunity.price_comparison_note or "")


def test_skhynix_ads_contract_uses_us_ads_one_to_one() -> None:
    ads_perp = _current("xyz", "xyz:SKHY", 0.00002, 0.00009)
    ads_perp.update(
        underlying="SKHY",
        display_name="SK Hynix ADS",
        mark_price=163.0,
        index_price=162.9,
    )

    opportunities = build_carry_opportunities(
        [ads_perp],
        [],
        lookback_days=7,
        spot_rows=[
            _spot("SKHY", 160.0, underlying="SKHY"),
            _spot(
                "000660.KS",
                1280.0,
                venue="kr_equity",
                underlying="SKHYNIX",
                market="KR",
                local_price=1_900_000,
                currency="KRW",
                local_per_usd=1484.375,
            ),
        ],
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.long_venue == "us_equity"
    assert opportunity.spot_market == "US"
    assert opportunity.spot_symbol == "SKHY"
    assert opportunity.spot_units_per_perp_unit == 1
    assert opportunity.spot_equivalent_price_usd == 160
    assert opportunity.spot_perp_basis_pct == pytest.approx(1.875)


def test_skhynix_common_share_does_not_fall_back_to_us_ads() -> None:
    row = _current("xyz", "xyz:SKHX", 0.00002, 0.00009)
    row.update(underlying="SKHYNIX", mark_price=1280.0, index_price=1279.0)

    assert build_carry_opportunities(
        [row],
        [],
        lookback_days=7,
        spot_rows=[_spot("SKHY", 160.0, underlying="SKHY")],
    ) == []


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


def test_liquidity_ignores_invalid_market_values() -> None:
    row = _current("xyz", "xyz:BB", 0.00002, 0.00009)
    row.update(
        underlying="BB",
        open_interest=float("nan"),
        volume_24h=-1,
    )

    opportunities = build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[_spot("BB", 100)]
    )

    assert len(opportunities) == 1
    assert opportunities[0].short_liquidity.open_interest_usd is None
    assert opportunities[0].short_liquidity.volume_24h_usd is None


@pytest.mark.parametrize(
    ("venue", "contract_symbol", "underlying", "spot_venue", "spot_symbol", "market", "currency", "local_price", "fx"),
    [
        ("xyz", "xyz:MINIMAX", "MINIMAX", "hk_equity", "0100.HK", "HK", "HKD", 235.2, 7.84),
        ("xyz", "xyz:SOFTBANK", "SOFTBANK", "jp_equity", "9984.T", "JP", "JPY", 6_574, 162.2),
    ],
)
def test_local_market_contracts_use_their_exact_hk_or_jp_security(
    venue: str,
    contract_symbol: str,
    underlying: str,
    spot_venue: str,
    spot_symbol: str,
    market: str,
    currency: str,
    local_price: float,
    fx: float,
) -> None:
    usd_price = local_price / fx
    row = _current(venue, contract_symbol, 0.00002, 0.00009)
    row.update(
        underlying=underlying,
        mark_price=usd_price * 1.01,
        index_price=usd_price,
    )

    opportunities = build_carry_opportunities(
        [row],
        [],
        lookback_days=7,
        spot_rows=[
            _spot(
                spot_symbol,
                usd_price,
                venue=spot_venue,
                underlying=underlying,
                market=market,
                local_price=local_price,
                currency=currency,
                local_per_usd=fx,
            )
        ],
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.spot_market == market
    assert opportunity.spot_symbol == spot_symbol
    assert opportunity.spot_mic in {"XHKG", "XTKS"}
    assert opportunity.spot_perp_basis_pct == pytest.approx(1.0)


def test_lighter_byd_does_not_accept_byd_company_and_reduce_only_is_not_actionable() -> None:
    row = _current("lighter", "BYD", 0.00002, 0.00009)
    row.update(underlying="BYD", mark_price=2.96, index_price=2.95)
    wrong_spot = _spot(
        "1211.HK",
        11.0,
        venue="hk_equity",
        underlying="BYD",
        market="HK",
        local_price=86.0,
        currency="HKD",
        local_per_usd=7.82,
    )
    assert build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[wrong_spot]
    ) == []

    row["force_reduce_only"] = True
    correct_spot = _spot(
        "0285.HK",
        2.96,
        venue="hk_equity",
        underlying="BYD",
        market="HK",
        local_price=23.2,
        currency="HKD",
        local_per_usd=7.84,
    )
    assert build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[correct_spot]
    ) == []


def test_local_common_share_and_us_ads_never_form_perp_pair_by_company_name() -> None:
    common = _current("lighter", "SKHYNIXUSD", 0.00003, 0.00009)
    common.update(underlying="SKHYNIX", mark_price=160, index_price=160)
    ads = _current("xyz", "xyz:SKHY", 0.00001, 0.00009)
    ads.update(underlying="SKHYNIX", mark_price=160, index_price=160)

    assert build_carry_opportunities(
        [common, ads], [], lookback_days=7, spot_rows=[]
    ) == []


def test_invalidated_spot_quote_is_not_reused() -> None:
    row = _current("xyz", "xyz:BB", 0.00002, 0.00009)
    row["underlying"] = "BB"
    spot = _spot("BB", 10.0)
    spot["quote_valid"] = False

    assert build_carry_opportunities(
        [row], [], lookback_days=7, spot_rows=[spot]
    ) == []
