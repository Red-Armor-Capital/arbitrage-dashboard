from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AssetClass = Literal["stock", "etf", "index", "preipo", "basket", "unknown"]


@dataclass(frozen=True)
class AssetSpec:
    underlying: str
    display_name: str
    asset_class: AssetClass
    spot_carry_eligible: bool


# Lighter does not expose a stable asset-class field in its public market API.
# This is the reviewed RWA/equity-like catalog from its live market directory.
LIGHTER_RWA_SYMBOLS = frozenset(
    {
        "AAPL", "AAOI", "AMD", "AMZN", "ANTHROPIC", "ARM", "ASML", "AVGO",
        "BABA", "BB", "BE", "BMNR", "BOT", "BOTZ", "BYD", "CBRS", "COIN",
        "CRCL", "CRWV", "DELL", "DRAM", "EWY", "GME", "GOOGL", "HOOD",
        "HYUNDAIUSD", "IBM", "INTC", "IWM", "LITE", "META", "MINIMAX",
        "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NOK", "NOW", "NVDA",
        "OPENAI", "ORCL", "PLTR", "POPMART", "QCOM", "QQQ", "RKLB",
        "SAMSUNGUSD", "SKHYNIXUSD", "SMIC", "SNDK", "SOXL", "SPCX", "SPY",
        "STRC", "TENCENT", "TSLA", "TSM", "TTWO", "URA", "US100", "US500",
        "WEN", "XIAOMI", "ZHIPU",
    }
)


HOTSTUFF_RWA_BASES = frozenset(
    {
        "AAPL", "AMZN", "EWJ", "EWY", "GOOGL", "META", "MSFT", "NVDA",
        "PLTR", "SPACEX", "TSLA", "USA100", "USA500",
    }
)


ORDERLY_NATIVE_RWA_SYMBOLS = frozenset(
    {"PERP_GOOGL_USDC", "PERP_NVDA_USDC", "PERP_TSLA_USDC"}
)
ORDERLY_MYTHOS_COMPANY_SYMBOLS = frozenset(
    {
        "AAOI", "AAPL", "AMD", "AMZN", "CBRS", "COIN", "CRCL", "GLW", "HOOD",
        "INTC", "LITE", "META", "MRVL", "MSFT", "MSTR", "NBIS", "SAMSUNG",
        "SKHYNIX", "SNDK", "SPCX", "WDC",
    }
)
ORDERLY_MYTHOS_ETF_SYMBOLS = frozenset(
    {"DRAM", "EWY", "KORU", "QQQ", "SOXL", "SPY"}
)


ALIASES = {
    "HYUNDAIUSD": "HYUNDAI",
    "SAMSUNGUSD": "SAMSUNG",
    "SMSN": "SAMSUNG",
    "SKHX": "SKHYNIX",
    "SKHYNIXUSD": "SKHYNIX",
    "SPCX": "SPACEX",
    "X": "SPACEX",
    "USA100": "US100",
    "USA500": "US500",
}

DISPLAY_NAMES = {
    "CBRS": "Cerebras Systems",
    "HYUNDAI": "Hyundai Motor",
    "SAMSUNG": "Samsung Electronics",
    "SKHYNIX": "SK Hynix",
    "SKHY": "SK Hynix ADS",
    "SPACEX": "SpaceX",
    "US100": "Nasdaq 100 Index",
    "US500": "S&P 500 Index",
}

ETF_SYMBOLS = frozenset(
    {
        "BOTZ", "DRAM", "EWJ", "EWT", "EWY", "EWZ", "IWM", "KORU", "QQQ",
        "SMH", "SOXL", "SPY", "URA", "URNM", "XLE",
    }
)
INDEX_SYMBOLS = frozenset({"US100", "US500"})
PREIPO_SYMBOLS = frozenset({"ANTHROPIC", "CBRS", "OPENAI", "SPACEX"})
BASKET_SYMBOLS = frozenset()


def resolve_asset(raw_symbol: str) -> AssetSpec:
    raw = raw_symbol.strip().upper()
    underlying = ALIASES.get(raw, raw)
    if underlying in ETF_SYMBOLS:
        asset_class: AssetClass = "etf"
    elif underlying in INDEX_SYMBOLS:
        asset_class = "index"
    elif underlying in PREIPO_SYMBOLS:
        asset_class = "preipo"
    elif underlying in BASKET_SYMBOLS:
        asset_class = "basket"
    else:
        asset_class = "stock"
    return AssetSpec(
        underlying=underlying,
        display_name=DISPLAY_NAMES.get(underlying, underlying),
        asset_class=asset_class,
        spot_carry_eligible=asset_class in {"stock", "etf"},
    )


def resolve_lighter_asset(symbol: str) -> AssetSpec | None:
    normalized = symbol.strip().upper()
    return resolve_asset(normalized) if normalized in LIGHTER_RWA_SYMBOLS else None


def resolve_hotstuff_asset(name: str, price_index: str | None = None) -> AssetSpec | None:
    index_base = str(price_index or "").split("/", 1)[0].strip().upper()
    name_base = name.strip().upper().removesuffix("-PERP")
    raw = index_base or name_base
    if raw not in HOTSTUFF_RWA_BASES:
        return None
    return resolve_asset(raw)


def orderly_base_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not value.startswith("PERP_"):
        return value
    value = value.removeprefix("PERP_")
    return value.split("_USDC", 1)[0]


def resolve_orderly_asset(
    symbol: str,
    display_symbol_name: str | None,
    broker_id: str | None,
) -> AssetSpec | None:
    normalized_symbol = symbol.strip().upper()
    base = str(display_symbol_name or orderly_base_symbol(normalized_symbol)).strip().upper()
    if normalized_symbol in ORDERLY_NATIVE_RWA_SYMBOLS:
        return resolve_asset(base)
    if str(broker_id or "").lower() != "mythos":
        return None
    if base not in ORDERLY_MYTHOS_COMPANY_SYMBOLS | ORDERLY_MYTHOS_ETF_SYMBOLS:
        return None
    return resolve_asset(base)


def asset_metadata(spec: AssetSpec, raw_underlying: str) -> dict[str, object]:
    return {
        "asset_class": spec.asset_class,
        "spot_carry_eligible": spec.spot_carry_eligible,
        "raw_underlying": raw_underlying,
        "classification_source": "venue_category_or_reviewed_registry",
    }
