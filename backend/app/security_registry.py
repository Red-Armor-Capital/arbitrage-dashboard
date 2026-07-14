from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SpotAssetClass = Literal["stock", "etf"]
ComparisonKind = Literal[
    "direct_usd",
    "local_fx",
    "local_currency",
    "quanto_reference",
]
ContractKey = tuple[str, str]


@dataclass(frozen=True)
class SecuritySpec:
    """One exact, executable exchange-listed security.

    ``underlying`` is retained only for display and company-level grouping. It
    is never a lookup key for contract-to-spot matching.
    """

    security_id: str
    underlying: str
    display_name: str
    asset_class: SpotAssetClass
    market: str
    mic: str
    ticker: str
    quote_symbols: tuple[str, ...]
    local_currency: str
    timezone: str
    spot_venue: str
    fx_symbol: str | None = None


@dataclass(frozen=True)
class ContractSpotLink:
    """Reviewed mapping from one exact DEX contract to one exact security."""

    venue: str
    symbol: str
    security_id: str
    spot_units_per_perp_unit: float = 1.0
    comparison_kind: ComparisonKind = "direct_usd"
    comparison_note: str | None = None


def normalize_contract_key(venue: str, symbol: str) -> ContractKey | None:
    normalized_venue = str(venue or "").strip().lower()
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_venue or not normalized_symbol:
        return None
    return normalized_venue, normalized_symbol


def validate_registry(
    securities: Iterable[SecuritySpec],
    links: Iterable[ContractSpotLink],
) -> None:
    security_rows = tuple(securities)
    link_rows = tuple(links)
    security_ids: set[str] = set()

    for spec in security_rows:
        if not spec.security_id or spec.security_id != spec.security_id.upper():
            raise ValueError(f"invalid security_id: {spec.security_id!r}")
        if spec.security_id in security_ids:
            raise ValueError(f"duplicate security_id: {spec.security_id}")
        security_ids.add(spec.security_id)
        if not spec.security_id.startswith(f"{spec.market}:{spec.mic}:"):
            raise ValueError(
                f"security_id does not match market/MIC: {spec.security_id}"
            )
        if spec.asset_class not in {"stock", "etf"}:
            raise ValueError(f"unsupported spot asset class: {spec.asset_class}")
        if not spec.ticker or not spec.quote_symbols or any(
            not value.strip() for value in spec.quote_symbols
        ):
            raise ValueError(f"missing quote symbol for {spec.security_id}")
        if len(set(spec.quote_symbols)) != len(spec.quote_symbols):
            raise ValueError(f"duplicate quote symbol for {spec.security_id}")
        if (
            not spec.market
            or not spec.mic
            or not spec.underlying
            or not spec.display_name
            or not spec.spot_venue
        ):
            raise ValueError(f"incomplete security spec: {spec.security_id}")
        if len(spec.local_currency) != 3 or spec.local_currency != spec.local_currency.upper():
            raise ValueError(f"invalid local currency for {spec.security_id}")
        if spec.local_currency != "USD" and not spec.fx_symbol:
            raise ValueError(f"missing FX mapping for {spec.security_id}")
        if spec.local_currency == "USD" and spec.fx_symbol is not None:
            raise ValueError(f"unexpected FX mapping for {spec.security_id}")
        try:
            ZoneInfo(spec.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"invalid timezone for {spec.security_id}: {spec.timezone}"
            ) from exc

    contract_keys: set[ContractKey] = set()
    allowed_comparisons = {
        "direct_usd",
        "local_fx",
        "local_currency",
        "quanto_reference",
    }
    for link in link_rows:
        key = normalize_contract_key(link.venue, link.symbol)
        if key is None:
            raise ValueError("contract link requires venue and symbol")
        if key in contract_keys:
            raise ValueError(f"duplicate contract mapping: {key[0]}:{key[1]}")
        contract_keys.add(key)
        if link.security_id not in security_ids:
            raise ValueError(
                f"unknown security_id for {key[0]}:{key[1]}: {link.security_id}"
            )
        if (
            not math.isfinite(link.spot_units_per_perp_unit)
            or link.spot_units_per_perp_unit <= 0
        ):
            raise ValueError(f"invalid contract multiplier for {key[0]}:{key[1]}")
        if link.comparison_kind not in allowed_comparisons:
            raise ValueError(f"invalid comparison kind for {key[0]}:{key[1]}")


_US_ETFS = frozenset(
    {
        "BOT",
        "BOTZ",
        "DRAM",
        "EWJ",
        "EWT",
        "EWY",
        "EWZ",
        "IWM",
        "KORU",
        "QQQ",
        "SMH",
        "SOXL",
        "SPY",
        "URA",
        "URNM",
        "XLE",
    }
)

_US_MICS = {
    "BABA": "XNYS",
    "BB": "XNYS",
    "BE": "XNYS",
    "BMNR": "XASE",
    "BX": "XNYS",
    "CRCL": "XNYS",
    "DELL": "XNYS",
    "GME": "XNYS",
    "HIMS": "XNYS",
    "IBM": "XNYS",
    "LLY": "XNYS",
    "NOK": "XNYS",
    "NOW": "XNYS",
    "ORCL": "XNYS",
    "TSM": "XNYS",
    "DRAM": "BATS",
    "EWJ": "ARCX",
    "EWT": "ARCX",
    "EWY": "ARCX",
    "EWZ": "ARCX",
    "IWM": "ARCX",
    "KORU": "ARCX",
    "SMH": "ARCX",
    "SOXL": "ARCX",
    "SPY": "ARCX",
    "URA": "ARCX",
    "URNM": "ARCX",
    "XLE": "ARCX",
}

_US_DISPLAY_NAMES = {
    "ARM": "Arm Holdings ADS",
    "ASML": "ASML Holding ADS",
    "BABA": "Alibaba Group ADS",
    "BOT": "BOT closed-end fund",
    "CBRS": "Cerebras Systems",
    "NOK": "Nokia ADR",
    "PURR": "PURR",
    "QNT": "QNT",
    "SKHY": "SK Hynix ADS",
    "TSM": "Taiwan Semiconductor Manufacturing ADS",
}

_US_TICKERS = (
    "AAOI",
    "AAPL",
    "AMAT",
    "AMD",
    "AMZN",
    "ARM",
    "ASML",
    "AVGO",
    "BABA",
    "BB",
    "BE",
    "BMNR",
    "BOT",
    "BOTZ",
    "BIRD",
    "BX",
    "CBRS",
    "COIN",
    "COST",
    "CRCL",
    "CRWV",
    "DELL",
    "DKNG",
    "DRAM",
    "EBAY",
    "EWJ",
    "EWT",
    "EWY",
    "EWZ",
    "GME",
    "GLW",
    "GOOG",
    "GOOGL",
    "HIMS",
    "HOOD",
    "IBM",
    "INTC",
    "IWM",
    "KORU",
    "LITE",
    "LLY",
    "META",
    "MRVL",
    "MSFT",
    "MSTR",
    "MU",
    "NBIS",
    "NFLX",
    "NOK",
    "NOW",
    "NVDA",
    "ORCL",
    "PLTR",
    "PURR",
    "QCOM",
    "QQQ",
    "QNT",
    "RIVN",
    "RKLB",
    "SKHY",
    "SHAZ",
    "SMH",
    "SNDK",
    "SOXL",
    "SPY",
    "STRC",
    "TSLA",
    "TSM",
    "TTWO",
    "URA",
    "URNM",
    "USAR",
    "WDC",
    "WEN",
    "XLE",
    "ZM",
)


def _us_security(ticker: str) -> SecuritySpec:
    mic = _US_MICS.get(ticker, "XNAS")
    return SecuritySpec(
        security_id=f"US:{mic}:{ticker}",
        underlying=ticker,
        display_name=_US_DISPLAY_NAMES.get(ticker, ticker),
        asset_class="etf" if ticker in _US_ETFS else "stock",
        market="US",
        mic=mic,
        ticker=ticker,
        quote_symbols=("SKHY", "SKHYV") if ticker == "SKHY" else (ticker,),
        local_currency="USD",
        timezone="America/New_York",
        spot_venue="us_equity",
    )


_LOCAL_SECURITIES = (
    SecuritySpec(
        "KR:XKRX:000660",
        "SKHYNIX",
        "SK Hynix common share",
        "stock",
        "KR",
        "XKRX",
        "000660.KS",
        ("000660.KS",),
        "KRW",
        "Asia/Seoul",
        "kr_equity",
        "KRW=X",
    ),
    SecuritySpec(
        "KR:XKRX:005930",
        "SAMSUNG",
        "Samsung Electronics common share",
        "stock",
        "KR",
        "XKRX",
        "005930.KS",
        ("005930.KS",),
        "KRW",
        "Asia/Seoul",
        "kr_equity",
        "KRW=X",
    ),
    SecuritySpec(
        "KR:XKRX:005380",
        "HYUNDAI",
        "Hyundai Motor common share",
        "stock",
        "KR",
        "XKRX",
        "005380.KS",
        ("005380.KS",),
        "KRW",
        "Asia/Seoul",
        "kr_equity",
        "KRW=X",
    ),
    SecuritySpec(
        "KR:XKRX:042700",
        "HANMI",
        "Hanmi Semiconductor common share",
        "stock",
        "KR",
        "XKRX",
        "042700.KS",
        ("042700.KS",),
        "KRW",
        "Asia/Seoul",
        "kr_equity",
        "KRW=X",
    ),
    SecuritySpec(
        "HK:XHKG:0100",
        "MINIMAX",
        "MiniMax Group",
        "stock",
        "HK",
        "XHKG",
        "0100.HK",
        ("0100.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "HK:XHKG:2513",
        "ZHIPU",
        "Knowledge Atlas Technology (Zhipu AI)",
        "stock",
        "HK",
        "XHKG",
        "2513.HK",
        ("2513.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "HK:XHKG:0700",
        "TENCENT",
        "Tencent Holdings",
        "stock",
        "HK",
        "XHKG",
        "0700.HK",
        ("0700.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "HK:XHKG:1810",
        "XIAOMI",
        "Xiaomi Corporation",
        "stock",
        "HK",
        "XHKG",
        "1810.HK",
        ("1810.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "HK:XHKG:0981",
        "SMIC",
        "Semiconductor Manufacturing International",
        "stock",
        "HK",
        "XHKG",
        "0981.HK",
        ("0981.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "HK:XHKG:9992",
        "POPMART",
        "Pop Mart International",
        "stock",
        "HK",
        "XHKG",
        "9992.HK",
        ("9992.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "HK:XHKG:0285",
        "BYD",
        "BYD Electronic (International)",
        "stock",
        "HK",
        "XHKG",
        "0285.HK",
        ("0285.HK",),
        "HKD",
        "Asia/Hong_Kong",
        "hk_equity",
        "HKD=X",
    ),
    SecuritySpec(
        "JP:XTKS:285A",
        "KIOXIA",
        "Kioxia Holdings",
        "stock",
        "JP",
        "XTKS",
        "285A.T",
        ("285A.T",),
        "JPY",
        "Asia/Tokyo",
        "jp_equity",
        "JPY=X",
    ),
    SecuritySpec(
        "JP:XTKS:9984",
        "SOFTBANK",
        "SoftBank Group",
        "stock",
        "JP",
        "XTKS",
        "9984.T",
        ("9984.T",),
        "JPY",
        "Asia/Tokyo",
        "jp_equity",
        "JPY=X",
    ),
)

SECURITIES = tuple(_us_security(ticker) for ticker in _US_TICKERS) + _LOCAL_SECURITIES


def _security_id_for_us(ticker: str) -> str:
    return f"US:{_US_MICS.get(ticker, 'XNAS')}:{ticker}"


_US_ADS = frozenset({"ARM", "ASML", "BABA", "NOK", "SKHY", "TSM"})
_US_COMPARISON_NOTES = {
    "BOT": "BOT contract maps to the Nasdaq-listed BOT closed-end fund",
    "CBRS": "CBRS contract maps to the now-public Nasdaq common share",
    "PURR": "Legacy PURRDAT contract symbol maps to the Nasdaq ticker PURR",
}


def _us_link(venue: str, symbol: str, ticker: str) -> ContractSpotLink:
    note = _US_COMPARISON_NOTES.get(ticker)
    if note is None and ticker in _US_ADS:
        note = (
            f"{ticker} contract maps 1:1 to the US-listed ADS/ADR, not the "
            "issuer's home-market ordinary share"
        )
    return ContractSpotLink(
        venue=venue,
        symbol=symbol,
        security_id=_security_id_for_us(ticker),
        comparison_note=note,
    )


_LIGHTER_US = (
    "AAOI", "AAPL", "AMD", "AMZN", "ARM", "ASML", "AVGO", "BABA", "BB",
    "BE", "BMNR", "BOT", "BOTZ", "CBRS", "COIN", "CRCL", "CRWV", "DELL",
    "DRAM", "EWY", "GME", "GOOGL", "HOOD", "IBM", "INTC", "IWM", "LITE",
    "META", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NOK", "NOW", "NVDA",
    "ORCL", "PLTR", "QCOM", "QNT", "QQQ", "RKLB", "SNDK", "SOXL", "SPY",
    "STRC", "TSLA", "TSM", "TTWO", "URA", "WEN",
)

_XYZ_US = (
    "AAPL", "AMAT", "AMD", "AMZN", "ARM", "ASML", "AVGO", "BABA", "BB",
    "BE", "BIRD", "BOT", "BX", "CBRS", "COIN", "COST", "CRCL", "CRWV",
    "DELL", "DKNG", "DRAM", "EBAY", "EWJ", "EWT", "EWY", "EWZ", "GME",
    "GOOGL", "HIMS", "HOOD", "IBM", "INTC", "LITE", "LLY", "META", "MRVL",
    "MSFT", "MSTR", "MU", "NBIS", "NFLX", "NOK", "NOW", "NVDA", "ORCL",
    "PLTR", "QCOM", "QNT", "RIVN", "RKLB", "SHAZ", "SKHY", "SMH", "SNDK",
    "STRC", "TSLA", "TSM", "URNM", "USAR", "WDC", "XLE", "ZM",
)

_HOTSTUFF_US = (
    "AAPL", "AMZN", "EWJ", "EWY", "GOOGL", "META", "MSFT", "NVDA", "PLTR",
    "TSLA",
)

_EXTENDED_US = {
    "AAPL_24_5-USD": "AAPL",
    "AMD_24_5-USD": "AMD",
    "AMZN_24_5-USD": "AMZN",
    "BABA_24_5-USD": "BABA",
    "COIN_24_5-USD": "COIN",
    "CRCL_24_5-USD": "CRCL",
    "GOOG_24_5-USD": "GOOG",
    "HOOD_24_5-USD": "HOOD",
    "INTC_24_5-USD": "INTC",
    "META_24_5-USD": "META",
    "MSFT_24_5-USD": "MSFT",
    "MSTR_24_5-USD": "MSTR",
    "MU_24_5-USD": "MU",
    "NVDA_24_5-USD": "NVDA",
    "ORCL_24_5-USD": "ORCL",
    "PLTR_24_5-USD": "PLTR",
    "SNDK_24_5-USD": "SNDK",
    "TSLA_24_5-USD": "TSLA",
}

_ORDERLY_US = {
    "PERP_AAOI_USDC_MYTHOS": "AAOI",
    "PERP_AAPL_USDC_MYTHOS": "AAPL",
    "PERP_AMD_USDC_MYTHOS": "AMD",
    "PERP_AMZN_USDC_MYTHOS": "AMZN",
    "PERP_CBRS_USDC_MYTHOS": "CBRS",
    "PERP_COIN_USDC_MYTHOS": "COIN",
    "PERP_CRCL_USDC_MYTHOS": "CRCL",
    "PERP_DRAM_USDC_MYTHOS": "DRAM",
    "PERP_EWY_USDC_MYTHOS": "EWY",
    "PERP_GLW_USDC_MYTHOS": "GLW",
    "PERP_GOOGL_USDC": "GOOGL",
    "PERP_HOOD_USDC_MYTHOS": "HOOD",
    "PERP_INTC_USDC_MYTHOS": "INTC",
    "PERP_KORU_USDC_MYTHOS": "KORU",
    "PERP_LITE_USDC_MYTHOS": "LITE",
    "PERP_META_USDC_MYTHOS": "META",
    "PERP_MRVL_USDC_MYTHOS": "MRVL",
    "PERP_MSFT_USDC_MYTHOS": "MSFT",
    "PERP_MSTR_USDC_MYTHOS": "MSTR",
    "PERP_NBIS_USDC_MYTHOS": "NBIS",
    "PERP_NVDA_USDC": "NVDA",
    "PERP_QQQ_USDC_MYTHOS": "QQQ",
    "PERP_SNDK_USDC_MYTHOS": "SNDK",
    "PERP_SOXL_USDC_MYTHOS": "SOXL",
    "PERP_SPY_USDC_MYTHOS": "SPY",
    "PERP_TSLA_USDC": "TSLA",
    "PERP_WDC_USDC_MYTHOS": "WDC",
}

_CONTRACT_LINKS: list[ContractSpotLink] = []
_CONTRACT_LINKS.extend(_us_link("lighter", ticker, ticker) for ticker in _LIGHTER_US)
_CONTRACT_LINKS.extend(_us_link("xyz", f"xyz:{ticker}", ticker) for ticker in _XYZ_US)
_CONTRACT_LINKS.extend(
    _us_link("hotstuff", f"{ticker}-PERP", ticker) for ticker in _HOTSTUFF_US
)
_CONTRACT_LINKS.extend(
    _us_link("extended", symbol, ticker) for symbol, ticker in _EXTENDED_US.items()
)
_CONTRACT_LINKS.extend(
    _us_link("orderly", symbol, ticker) for symbol, ticker in _ORDERLY_US.items()
)
_CONTRACT_LINKS.append(_us_link("xyz", "xyz:PURRDAT", "PURR"))

_CONTRACT_LINKS.extend(
    (
        ContractSpotLink(
            "lighter",
            "SKHYNIXUSD",
            "KR:XKRX:000660",
            comparison_kind="quanto_reference",
            comparison_note=(
                "SK Hynix KRX common share converted from KRW to USD; "
                "Lighter USD contract is a quanto reference"
            ),
        ),
        ContractSpotLink("xyz", "xyz:SKHX", "KR:XKRX:000660", comparison_kind="local_fx"),
        ContractSpotLink(
            "orderly",
            "PERP_SKHYNIX_USDC_mythos",
            "KR:XKRX:000660",
            comparison_kind="local_fx",
        ),
        ContractSpotLink(
            "lighter",
            "SAMSUNGUSD",
            "KR:XKRX:005930",
            comparison_kind="quanto_reference",
        ),
        ContractSpotLink("xyz", "xyz:SMSN", "KR:XKRX:005930", comparison_kind="local_fx"),
        ContractSpotLink(
            "orderly",
            "PERP_SAMSUNG_USDC_mythos",
            "KR:XKRX:005930",
            comparison_kind="local_fx",
        ),
        ContractSpotLink(
            "lighter",
            "HYUNDAIUSD",
            "KR:XKRX:005380",
            comparison_kind="quanto_reference",
        ),
        ContractSpotLink("xyz", "xyz:HYUNDAI", "KR:XKRX:005380", comparison_kind="local_fx"),
        # The retired Lighter HANMI contract was KRW-denominated. Keeping that
        # distinction explicit prevents a future reactivation from being
        # treated as a USD contract without a conversion step.
        ContractSpotLink(
            "lighter",
            "HANMI",
            "KR:XKRX:042700",
            comparison_kind="local_currency",
            comparison_note=(
                "Hanmi Semiconductor 042700.KS; legacy Lighter contract price "
                "is KRW-denominated"
            ),
        ),
        ContractSpotLink("lighter", "MINIMAX", "HK:XHKG:0100", comparison_kind="local_fx"),
        ContractSpotLink("xyz", "xyz:MINIMAX", "HK:XHKG:0100", comparison_kind="local_fx"),
        ContractSpotLink("lighter", "ZHIPU", "HK:XHKG:2513", comparison_kind="local_fx"),
        ContractSpotLink("xyz", "xyz:ZHIPU", "HK:XHKG:2513", comparison_kind="local_fx"),
        ContractSpotLink("lighter", "TENCENT", "HK:XHKG:0700", comparison_kind="local_fx"),
        ContractSpotLink("lighter", "XIAOMI", "HK:XHKG:1810", comparison_kind="local_fx"),
        ContractSpotLink("lighter", "SMIC", "HK:XHKG:0981", comparison_kind="local_fx"),
        ContractSpotLink("lighter", "POPMART", "HK:XHKG:9992", comparison_kind="local_fx"),
        ContractSpotLink(
            "lighter",
            "BYD",
            "HK:XHKG:0285",
            comparison_kind="local_fx",
            comparison_note=(
                "Lighter BYD tracks one share of BYD Electronic 0285.HK, "
                "not BYD Company 1211.HK"
            ),
        ),
        ContractSpotLink("xyz", "xyz:KIOXIA", "JP:XTKS:285A", comparison_kind="local_fx"),
        ContractSpotLink("xyz", "xyz:SOFTBANK", "JP:XTKS:9984", comparison_kind="local_fx"),
    )
)

CONTRACT_SPOT_LINKS = tuple(_CONTRACT_LINKS)
validate_registry(SECURITIES, CONTRACT_SPOT_LINKS)

SECURITIES_BY_ID = MappingProxyType({spec.security_id: spec for spec in SECURITIES})
CONTRACT_SPOT_LINKS_BY_KEY = MappingProxyType(
    {
        normalize_contract_key(link.venue, link.symbol): link
        for link in CONTRACT_SPOT_LINKS
    }
)


def get_security(security_id: str) -> SecuritySpec | None:
    return SECURITIES_BY_ID.get(str(security_id or "").strip().upper())


def resolve_contract_spot(venue: str, symbol: str) -> ContractSpotLink | None:
    key = normalize_contract_key(venue, symbol)
    return CONTRACT_SPOT_LINKS_BY_KEY.get(key) if key is not None else None


def resolve_contract_security(venue: str, symbol: str) -> SecuritySpec | None:
    link = resolve_contract_spot(venue, symbol)
    return get_security(link.security_id) if link is not None else None


def securities_for_contracts(
    contract_keys: Iterable[ContractKey],
) -> tuple[SecuritySpec, ...]:
    seen: set[str] = set()
    result: list[SecuritySpec] = []
    for venue, symbol in contract_keys:
        spec = resolve_contract_security(venue, symbol)
        if spec is None or spec.security_id in seen:
            continue
        seen.add(spec.security_id)
        result.append(spec)
    return tuple(result)
