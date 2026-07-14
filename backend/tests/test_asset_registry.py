import pytest

from backend.app.asset_registry import (
    asset_metadata,
    resolve_asset,
    resolve_hotstuff_asset,
    resolve_lighter_asset,
    resolve_orderly_asset,
)


@pytest.mark.parametrize(
    ("raw_symbol", "canonical"),
    [
        ("SKHX", "SKHYNIX"),
        ("SKHYNIXUSD", "SKHYNIX"),
        ("SMSN", "SAMSUNG"),
        ("SAMSUNGUSD", "SAMSUNG"),
        ("HYUNDAIUSD", "HYUNDAI"),
        ("PURRDAT", "PURR"),
        ("SPCX", "SPACEX"),
        ("X", "SPACEX"),
        ("USA100", "US100"),
        ("USA500", "US500"),
    ],
)
def test_resolve_asset_applies_only_reviewed_aliases(
    raw_symbol: str,
    canonical: str,
) -> None:
    assert resolve_asset(raw_symbol).underlying == canonical


def test_similar_exchange_symbols_remain_distinct() -> None:
    assert resolve_asset("SKHY").underlying == "SKHY"
    assert resolve_asset("GOOG").underlying == "GOOG"
    assert resolve_asset("GOOGL").underlying == "GOOGL"


@pytest.mark.parametrize("symbol", ["INTC", "SKHX", "SAMSUNGUSD", "CBRS"])
def test_stocks_are_spot_carry_eligible(symbol: str) -> None:
    spec = resolve_asset(symbol)
    assert spec.asset_class == "stock"
    assert spec.spot_carry_eligible is True


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "DRAM", "EWY"])
def test_etfs_are_spot_carry_eligible(symbol: str) -> None:
    spec = resolve_asset(symbol)
    assert spec.asset_class == "etf"
    assert spec.spot_carry_eligible is True


@pytest.mark.parametrize(
    ("symbol", "asset_class"),
    [
        ("US100", "index"),
        ("USA500", "index"),
        ("SPCX", "preipo"),
        ("ANTHROPIC", "preipo"),
    ],
)
def test_non_broker_spot_assets_are_not_spot_carry_eligible(
    symbol: str,
    asset_class: str,
) -> None:
    spec = resolve_asset(symbol)
    assert spec.asset_class == asset_class
    assert spec.spot_carry_eligible is False


def test_lighter_registry_is_an_exact_admission_filter() -> None:
    assert resolve_lighter_asset("INTC") is not None
    assert resolve_lighter_asset("SKHYNIXUSD").underlying == "SKHYNIX"
    assert resolve_lighter_asset("BTC") is None


def test_hotstuff_uses_price_index_to_resolve_opaque_contract_name() -> None:
    spec = resolve_hotstuff_asset("X-PERP", "SPACEX/USD")
    assert spec is not None
    assert spec.underlying == "SPACEX"
    assert spec.asset_class == "preipo"
    assert resolve_hotstuff_asset("BTC-PERP", "BTC/USD") is None


def test_orderly_accepts_native_and_reviewed_mythos_assets_only() -> None:
    native = resolve_orderly_asset("PERP_NVDA_USDC", None, None)
    mythos_stock = resolve_orderly_asset(
        "PERP_SKHYNIX_USDC", "SKHYNIX", "mythos"
    )
    mythos_etf = resolve_orderly_asset("PERP_DRAM_USDC", "DRAM", "MYTHOS")

    assert native is not None and native.underlying == "NVDA"
    assert mythos_stock is not None and mythos_stock.underlying == "SKHYNIX"
    assert mythos_etf is not None and mythos_etf.asset_class == "etf"
    assert resolve_orderly_asset("PERP_BTC_USDC", "BTC", "mythos") is None
    assert resolve_orderly_asset("PERP_INTC_USDC", "INTC", "other") is None


def test_asset_metadata_preserves_classification_and_raw_symbol() -> None:
    metadata = asset_metadata(resolve_asset("SKHX"), "SKHX")

    assert metadata == {
        "asset_class": "stock",
        "spot_carry_eligible": True,
        "raw_underlying": "SKHX",
        "classification_source": "venue_category_or_reviewed_registry",
    }
