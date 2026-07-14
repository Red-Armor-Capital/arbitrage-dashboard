import pytest

from backend.app.asset_registry import LIGHTER_RWA_SYMBOLS
from backend.app.security_registry import (
    CONTRACT_SPOT_LINKS,
    SECURITIES,
    ContractSpotLink,
    SecuritySpec,
    get_security,
    resolve_contract_security,
    resolve_contract_spot,
    securities_for_contracts,
    validate_registry,
)


def test_xyz_korean_common_share_and_us_ads_are_distinct() -> None:
    common = resolve_contract_spot("xyz", "xyz:SKHX")
    ads = resolve_contract_spot("xyz", "xyz:SKHY")

    assert common is not None and common.security_id == "KR:XKRX:000660"
    assert ads is not None and ads.security_id == "US:XNAS:SKHY"
    assert common.security_id != ads.security_id
    assert resolve_contract_security("xyz", "xyz:SKHX").ticker == "000660.KS"
    assert resolve_contract_security("xyz", "xyz:SKHY").ticker == "SKHY"


def test_contract_resolution_is_exact_and_fail_closed() -> None:
    assert resolve_contract_spot("xyz", "xyz:AAPL") is not None
    assert resolve_contract_spot("lighter", "AAPL") is not None

    # Venue and the complete venue-native symbol are both part of identity.
    assert resolve_contract_spot("unknown", "AAPL") is None
    assert resolve_contract_spot("lighter", "xyz:AAPL") is None
    assert resolve_contract_spot("xyz", "AAPL") is None
    assert resolve_contract_spot("lighter", "SKHY") is None
    assert resolve_contract_spot("xyz", "xyz:DOES_NOT_EXIST") is None
    assert resolve_contract_spot("", "AAPL") is None

    # Only insignificant case and surrounding whitespace are normalized.
    normalized = resolve_contract_spot(" XYZ ", " xyz:skhx ")
    assert normalized is not None
    assert normalized.security_id == "KR:XKRX:000660"


@pytest.mark.parametrize(
    ("venue", "symbol", "security_id", "ticker"),
    [
        ("lighter", "SKHYNIXUSD", "KR:XKRX:000660", "000660.KS"),
        ("orderly", "PERP_SAMSUNG_USDC_mythos", "KR:XKRX:005930", "005930.KS"),
        ("xyz", "xyz:HYUNDAI", "KR:XKRX:005380", "005380.KS"),
        ("lighter", "HANMI", "KR:XKRX:042700", "042700.KS"),
        ("lighter", "MINIMAX", "HK:XHKG:0100", "0100.HK"),
        ("xyz", "xyz:ZHIPU", "HK:XHKG:2513", "2513.HK"),
        ("lighter", "TENCENT", "HK:XHKG:0700", "0700.HK"),
        ("lighter", "XIAOMI", "HK:XHKG:1810", "1810.HK"),
        ("lighter", "SMIC", "HK:XHKG:0981", "0981.HK"),
        ("lighter", "POPMART", "HK:XHKG:9992", "9992.HK"),
        ("xyz", "xyz:KIOXIA", "JP:XTKS:285A", "285A.T"),
        ("xyz", "xyz:SOFTBANK", "JP:XTKS:9984", "9984.T"),
    ],
)
def test_reviewed_local_market_mappings(
    venue: str,
    symbol: str,
    security_id: str,
    ticker: str,
) -> None:
    link = resolve_contract_spot(venue, symbol)
    security = resolve_contract_security(venue, symbol)

    assert link is not None and link.security_id == security_id
    assert security is not None and security.ticker == ticker
    assert security.local_currency in {"KRW", "HKD", "JPY"}
    assert security.fx_symbol is not None


def test_lighter_byd_is_byd_electronic_not_byd_company() -> None:
    link = resolve_contract_spot("lighter", "BYD")
    security = resolve_contract_security("lighter", "BYD")

    assert link is not None
    assert security is not None
    assert security.security_id == "HK:XHKG:0285"
    assert security.ticker == "0285.HK"
    assert "BYD Electronic" in security.display_name
    assert "BYD Electronic" in (link.comparison_note or "")
    assert "1211.HK" in (link.comparison_note or "")


@pytest.mark.parametrize(
    ("venue", "symbol", "ticker"),
    [
        ("xyz", "xyz:PURRDAT", "PURR"),
        ("xyz", "xyz:SHAZ", "SHAZ"),
        ("xyz", "xyz:BIRD", "BIRD"),
        ("xyz", "xyz:QNT", "QNT"),
        ("lighter", "QNT", "QNT"),
        ("xyz", "xyz:BOT", "BOT"),
        ("lighter", "BOT", "BOT"),
        ("xyz", "xyz:CBRS", "CBRS"),
        ("lighter", "CBRS", "CBRS"),
        ("orderly", "PERP_CBRS_USDC_mythos", "CBRS"),
    ],
)
def test_reviewed_special_us_contract_symbols(
    venue: str,
    symbol: str,
    ticker: str,
) -> None:
    security = resolve_contract_security(venue, symbol)

    assert security is not None
    assert security.market == "US"
    assert security.mic == "XNAS"
    assert security.ticker == ticker


def test_bot_is_explicitly_the_listed_fund_not_an_inferred_token() -> None:
    link = resolve_contract_spot("lighter", "BOT")
    security = resolve_contract_security("lighter", "BOT")

    assert link is not None
    assert security is not None
    assert security.asset_class == "etf"
    assert "closed-end fund" in security.display_name
    assert "closed-end fund" in (link.comparison_note or "")


@pytest.mark.parametrize("ticker", ["ARM", "ASML", "BABA", "NOK", "SKHY", "TSM"])
def test_us_ads_contracts_map_to_us_listed_security(ticker: str) -> None:
    symbol = f"xyz:{ticker}" if ticker != "SKHY" else "xyz:SKHY"
    link = resolve_contract_spot("xyz", symbol)
    security = resolve_contract_security("xyz", symbol)

    assert link is not None
    assert security is not None
    assert security.market == "US"
    assert security.ticker == ticker
    assert security.local_currency == "USD"
    assert "home-market ordinary share" in (link.comparison_note or "")


def test_registry_deduplicates_securities_across_contracts() -> None:
    specs = securities_for_contracts(
        [
            ("lighter", "SKHYNIXUSD"),
            ("xyz", "xyz:SKHX"),
            ("orderly", "PERP_SKHYNIX_USDC_mythos"),
            ("xyz", "xyz:SKHY"),
            ("unknown", "SKHYNIXUSD"),
        ]
    )

    assert [spec.security_id for spec in specs] == [
        "KR:XKRX:000660",
        "US:XNAS:SKHY",
    ]


def _security(**changes) -> SecuritySpec:
    values = {
        "security_id": "US:XNAS:TEST",
        "underlying": "TEST",
        "display_name": "Test security",
        "asset_class": "stock",
        "market": "US",
        "mic": "XNAS",
        "ticker": "TEST",
        "quote_symbols": ("TEST",),
        "local_currency": "USD",
        "timezone": "America/New_York",
        "spot_venue": "us_equity",
        "fx_symbol": None,
    }
    values.update(changes)
    return SecuritySpec(**values)


def test_registry_rejects_duplicate_and_invalid_mappings() -> None:
    security = _security()
    link = ContractSpotLink("xyz", "xyz:TEST", security.security_id)

    validate_registry([security], [link])

    with pytest.raises(ValueError, match="duplicate security_id"):
        validate_registry([security, security], [link])
    with pytest.raises(ValueError, match="duplicate contract mapping"):
        validate_registry([security], [link, link])
    with pytest.raises(ValueError, match="unknown security_id"):
        validate_registry(
            [security],
            [ContractSpotLink("xyz", "xyz:OTHER", "US:XNAS:OTHER")],
        )
    with pytest.raises(ValueError, match="invalid contract multiplier"):
        validate_registry(
            [security],
            [ContractSpotLink("xyz", "xyz:TEST", security.security_id, 0)],
        )
    with pytest.raises(ValueError, match="missing FX mapping"):
        validate_registry(
            [_security(local_currency="JPY", security_id="JP:XTKS:TEST", market="JP", mic="XTKS")],
            [],
        )


def test_shipped_registry_validates_and_indices_are_complete() -> None:
    validate_registry(SECURITIES, CONTRACT_SPOT_LINKS)
    assert len({spec.security_id for spec in SECURITIES}) == len(SECURITIES)
    assert get_security("KR:XKRX:000660").ticker == "000660.KS"


def test_reviewed_lighter_catalog_is_fully_mapped_or_explicitly_excluded() -> None:
    excluded_without_listed_spot = {
        "ANTHROPIC",
        "OPENAI",
        "SPCX",
        "US100",
        "US500",
    }
    unmapped = {
        symbol
        for symbol in LIGHTER_RWA_SYMBOLS
        if resolve_contract_spot("lighter", symbol) is None
    }

    assert unmapped == excluded_without_listed_spot
