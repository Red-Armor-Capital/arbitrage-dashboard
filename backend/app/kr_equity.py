from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from .models import AdapterResult, Instrument, MarketSnapshot
from .us_equity import EquityQuote, fetch_yahoo_quote


KR_EQUITY_VENUE = "kr_equity"
USD_KRW_SYMBOL = "KRW=X"
NAVER_USD_KRW_CODE = "FX_USDKRW"


@dataclass(frozen=True)
class KrSpotSpec:
    underlying: str
    ticker: str
    quote_symbols: tuple[str, ...]
    display_name: str
    spot_units_per_perp_unit: float = 1.0


@dataclass(frozen=True)
class KrEquityCollection:
    result: AdapterResult
    errors: tuple[str, ...] = ()


def get_kr_spot_spec(underlying: str) -> KrSpotSpec | None:
    normalized = underlying.strip().upper()
    values = {
        "SKHYNIX": ("000660.KS", "SK Hynix · KRX common share"),
        "SAMSUNG": ("005930.KS", "Samsung Electronics · KRX common share"),
        "HYUNDAI": ("005380.KS", "Hyundai Motor · KRX common share"),
        "HANMI": ("042700.KS", "Hanmi Semiconductor · KRX common share"),
    }
    value = values.get(normalized)
    if value is None:
        return None
    ticker, display_name = value
    return KrSpotSpec(normalized, ticker, (ticker,), display_name)


def _number(value: object) -> float:
    parsed = float(str(value or "").replace(",", ""))
    if parsed <= 0:
        raise ValueError(f"invalid non-positive quote: {value}")
    return parsed


def _local_timestamp(value: object, zone_id: object = "Asia/Seoul") -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing localTradedAt")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(str(zone_id or "Asia/Seoul")))
    return parsed.astimezone(timezone.utc)


async def _naver_stock_quote(
    client: httpx.AsyncClient,
    spec: KrSpotSpec,
) -> EquityQuote:
    code = spec.ticker.split(".", 1)[0]
    response = await client.get(
        f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
    )
    response.raise_for_status()
    rows = response.json().get("datas") or []
    if not rows or not isinstance(rows[0], dict):
        raise ValueError("Naver returned no Korean stock quote")
    row: dict[str, Any] = rows[0]
    exchange = row.get("stockExchangeType") or {}
    regular_price = _number(row.get("closePriceRaw") or row.get("closePrice"))
    regular_at = _local_timestamp(
        row.get("localTradedAt"), exchange.get("zoneId")
    )
    regular_session = (
        "regular" if str(row.get("marketStatus") or "").upper() == "OPEN" else "closed"
    )

    price = regular_price
    observed_at = regular_at
    session = regular_session
    over = row.get("overMarketPriceInfo") or {}
    over_type = str(over.get("tradingSessionType") or "").upper()
    try:
        over_price = _number(over.get("overPrice"))
        over_at = _local_timestamp(over.get("localTradedAt"), exchange.get("zoneId"))
    except (TypeError, ValueError):
        over_price = None
        over_at = None
    over_status = str(over.get("overMarketStatus") or "").upper()
    trade_stop = str((over.get("tradeStopType") or {}).get("name") or "").upper()
    same_seoul_day = (
        over_at is not None
        and over_at.astimezone(ZoneInfo("Asia/Seoul")).date()
        == regular_at.astimezone(ZoneInfo("Asia/Seoul")).date()
    )
    valid_over = (
        over_price is not None
        and over_at is not None
        and over_type in {"PRE_MARKET", "AFTER_MARKET"}
        and same_seoul_day
    )
    regular_is_live_and_newer = regular_session == "regular" and (
        over_at is None or regular_at >= over_at
    )
    if valid_over and not regular_is_live_and_newer and (
        (over_status == "OPEN" and trade_stop == "TRADING" and over_at >= regular_at)
        or over_at > regular_at
    ):
        price = over_price
        observed_at = over_at
        session = "pre" if over_type == "PRE_MARKET" else "post"

    return EquityQuote(
        ticker=spec.ticker,
        provider_symbol=code,
        price=price,
        observed_at=observed_at,
        source="Naver Finance",
        session=session,
        is_delayed=int(exchange.get("delayTime") or 0) > 0,
    )


async def _naver_fx_quote(client: httpx.AsyncClient) -> EquityQuote:
    response = await client.get(
        "https://m.stock.naver.com/front-api/marketIndex/productDetail",
        params={"category": "exchange", "reutersCode": NAVER_USD_KRW_CODE},
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") or {}
    if payload.get("isSuccess") is not True or result.get("reutersCode") != NAVER_USD_KRW_CODE:
        raise ValueError("Naver returned no USD/KRW quote")
    exchange = result.get("stockExchangeType") or {}
    return EquityQuote(
        ticker=USD_KRW_SYMBOL,
        provider_symbol=NAVER_USD_KRW_CODE,
        price=_number(result.get("calcPrice") or result.get("closePrice")),
        observed_at=_local_timestamp(
            result.get("localTradedAt"), exchange.get("zoneId")
        ),
        source="Naver Finance · Hana Bank notice FX",
        session=(
            "regular"
            if str(result.get("marketStatus") or "").upper() == "OPEN"
            else "closed"
        ),
        is_delayed=False,
    )


async def _try_quote(
    label: str,
    loader: Callable[[], Awaitable[EquityQuote]],
) -> tuple[EquityQuote | None, str | None]:
    try:
        async with asyncio.timeout(12):
            return await loader(), None
    except Exception as exc:
        error = f"{label}: {type(exc).__name__}: {exc}"
        return None, error[-500:]


async def _stock_quote(
    client: httpx.AsyncClient,
    spec: KrSpotSpec,
) -> tuple[EquityQuote | None, str | None]:
    quote, _naver_error = await _try_quote(
        f"{spec.ticker}/Naver", lambda: _naver_stock_quote(client, spec)
    )
    if quote is not None:
        return quote, None
    fallback, yahoo_error = await _try_quote(
        f"{spec.ticker}/Yahoo", lambda: fetch_yahoo_quote(client, spec.ticker)
    )
    return fallback, yahoo_error if fallback is None else None


async def _fx_quote(
    client: httpx.AsyncClient,
) -> tuple[EquityQuote | None, str | None]:
    quote, _naver_error = await _try_quote(
        f"{USD_KRW_SYMBOL}/Naver", lambda: _naver_fx_quote(client)
    )
    if quote is not None:
        return quote, None
    fallback, yahoo_error = await _try_quote(
        f"{USD_KRW_SYMBOL}/Yahoo", lambda: fetch_yahoo_quote(client, USD_KRW_SYMBOL)
    )
    return fallback, yahoo_error if fallback is None else None


def _combined_source(stock_quote: EquityQuote, fx_quote: EquityQuote) -> str:
    fx_label = (
        "Hana Bank USD/KRW"
        if "Hana Bank" in fx_quote.source
        else f"{fx_quote.source} USD/KRW"
    )
    return f"{stock_quote.source} · {fx_label}"


async def collect_kr_equity_quotes(
    client: httpx.AsyncClient,
    underlyings: set[str],
) -> KrEquityCollection:
    specs = [
        spec
        for underlying in sorted(underlyings)
        if (spec := get_kr_spot_spec(underlying)) is not None
    ]
    if not specs:
        return KrEquityCollection(result=AdapterResult())

    equity_outcomes, fx_outcome = await asyncio.gather(
        asyncio.gather(*(_stock_quote(client, spec) for spec in specs)),
        _fx_quote(client),
    )
    fx_quote, fx_error = fx_outcome
    fx_valid = fx_quote is not None and fx_quote.price > 0

    instruments: list[Instrument] = []
    snapshots: list[MarketSnapshot] = []
    errors: list[str] = []
    if not fx_valid:
        errors.append(fx_error or f"{USD_KRW_SYMBOL}: invalid USD/KRW quote")
    for spec, (quote, error) in zip(specs, equity_outcomes, strict=True):
        quote_valid = quote is not None and fx_valid
        metadata: dict[str, object] = {
            "quote_source": (
                _combined_source(quote, fx_quote)
                if quote is not None and fx_quote is not None
                else None
            ),
            "provider_symbol": (
                quote.provider_symbol if quote else spec.quote_symbols[0]
            ),
            "quote_session": quote.session if quote else "unavailable",
            "quote_delayed": (
                (quote.is_delayed or fx_quote.is_delayed)
                if quote is not None and fx_quote is not None
                else True
            ),
            "spot_market": "KR",
            "market": "KR",
            "ticker": spec.ticker,
            "mic": "XKRX",
            "local_price": quote.price if quote else None,
            "local_currency": "KRW",
            "local_per_usd": fx_quote.price if fx_quote is not None else None,
            "fx_symbol": USD_KRW_SYMBOL,
            "fx_source": fx_quote.source if fx_quote is not None else None,
            "fx_observed_at": (
                fx_quote.observed_at.isoformat() if fx_quote is not None else None
            ),
            "stock_observed_at": quote.observed_at.isoformat() if quote else None,
            "quote_valid": quote_valid,
            "delay_status": (
                "unavailable"
                if not quote_valid
                else "delayed"
                if quote.is_delayed or fx_quote.is_delayed
                else "realtime"
            ),
        }
        instruments.append(
            Instrument(
                venue=KR_EQUITY_VENUE,
                symbol=spec.ticker,
                underlying=spec.underlying,
                display_name=spec.display_name,
                product_type="stock",
                quote_currency="USD",
                metadata=metadata,
            )
        )
        if not quote_valid:
            if error:
                errors.append(f"{spec.ticker}: {error}")
            continue

        assert quote is not None and fx_quote is not None
        usd_price = quote.price / fx_quote.price
        observed_at = min(quote.observed_at, fx_quote.observed_at)
        snapshots.append(
            MarketSnapshot(
                venue=KR_EQUITY_VENUE,
                symbol=spec.ticker,
                underlying=spec.underlying,
                observed_at=observed_at,
                source_observed_at=observed_at,
                bid=None,
                ask=None,
                mark_price=usd_price,
                index_price=usd_price,
            )
        )

    return KrEquityCollection(
        result=AdapterResult(instruments=instruments, snapshots=snapshots),
        errors=tuple(errors),
    )
