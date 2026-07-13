from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .models import AdapterResult, Instrument, MarketSnapshot


US_EQUITY_VENUE = "us_equity"


@dataclass(frozen=True)
class UsSpotSpec:
    underlying: str
    ticker: str
    quote_symbols: tuple[str, ...]
    spot_units_per_perp_unit: float = 1.0


@dataclass(frozen=True)
class UsEquityQuote:
    ticker: str
    provider_symbol: str
    price: float
    observed_at: datetime
    source: str
    session: str
    is_delayed: bool


@dataclass(frozen=True)
class UsEquityCollection:
    result: AdapterResult
    errors: tuple[str, ...] = ()


# Explicit allow-list: classification as a stock/ETF does not by itself prove
# that a DEX contract has an equivalent, publicly traded US spot leg.
_ONE_TO_ONE_US_TICKERS = frozenset(
    {
        "AAPL", "AAOI", "AMD", "AMZN", "ARM", "ASML", "AVGO", "BABA",
        "BB", "BE", "BMNR", "COIN", "CRCL", "CRWV", "DELL", "GME",
        "GOOGL", "HOOD", "IBM", "INTC", "IWM", "LITE", "META", "MRVL",
        "MSFT", "MSTR", "MU", "NBIS", "NOK", "NOW", "NVDA", "ORCL",
        "PLTR", "QCOM", "QQQ", "RKLB", "SNDK", "SOXL", "SPY", "STRC",
        "TSLA", "TSM", "TTWO", "URA", "WDC", "WEN",
    }
)


def get_us_spot_spec(underlying: str) -> UsSpotSpec | None:
    normalized = underlying.strip().upper()
    if normalized == "SKHYNIX":
        # One SKHY ADS represents 0.1 SK Hynix common share.
        return UsSpotSpec(normalized, "SKHY", ("SKHY", "SKHYV"), 10.0)
    if normalized == "SKHY":
        return UsSpotSpec(normalized, "SKHY", ("SKHY", "SKHYV"), 1.0)
    if normalized in _ONE_TO_ONE_US_TICKERS:
        return UsSpotSpec(normalized, normalized, (normalized,), 1.0)
    return None


def _timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _session_for_timestamp(meta: dict[str, Any], timestamp: int) -> str:
    periods = meta.get("currentTradingPeriod") or {}
    for name in ("pre", "regular", "post"):
        period = periods.get(name) or {}
        if int(period.get("start") or 0) <= timestamp <= int(period.get("end") or 0):
            return name
    return str(meta.get("marketState") or "closed").lower()


async def _yahoo_quote(client: httpx.AsyncClient, ticker: str) -> UsEquityQuote:
    response = await client.get(
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"interval": "1m", "range": "1d", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0 equity-carry-monitor/0.1"},
    )
    response.raise_for_status()
    chart = response.json().get("chart") or {}
    error = chart.get("error")
    results = chart.get("result") or []
    if error or not results:
        raise ValueError(str(error or "empty Yahoo chart result"))

    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote_blocks = ((result.get("indicators") or {}).get("quote") or [{}])
    closes = quote_blocks[0].get("close") or []
    candidates: list[tuple[int, float]] = []
    for stamp, close in zip(timestamps, closes, strict=False):
        try:
            price = float(close)
            stamp_value = int(stamp)
        except (TypeError, ValueError):
            continue
        if price > 0:
            candidates.append((stamp_value, price))

    regular_price = meta.get("regularMarketPrice")
    regular_time = meta.get("regularMarketTime")
    try:
        if float(regular_price) > 0 and int(regular_time) > 0:
            candidates.append((int(regular_time), float(regular_price)))
    except (TypeError, ValueError):
        pass
    if not candidates:
        raise ValueError("Yahoo returned no valid price")

    observed_timestamp, price = max(candidates, key=lambda item: item[0])
    observed_at = _timestamp(observed_timestamp)
    if observed_at is None:
        raise ValueError("Yahoo returned an invalid quote timestamp")
    return UsEquityQuote(
        ticker=ticker,
        provider_symbol=ticker,
        price=price,
        observed_at=observed_at,
        source="Yahoo chart",
        session=_session_for_timestamp(meta, observed_timestamp),
        is_delayed=False,
    )


def _parse_nasdaq_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for suffix in (" ET", " EST", " EDT"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        local = datetime.strptime(raw, "%b %d, %Y %I:%M %p")
    except ValueError:
        try:
            # Nasdaq's delayed/closed response often exposes only the trade
            # date. Last-sale is a regular-session close, so use 16:00 ET.
            local = datetime.strptime(raw, "%b %d, %Y").replace(hour=16)
        except ValueError:
            return None
    return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


async def _nasdaq_quote(client: httpx.AsyncClient, ticker: str) -> UsEquityQuote:
    response = await client.get(
        f"https://api.nasdaq.com/api/quote/{ticker}/info",
        params={"assetclass": "stocks"},
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}",
        },
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    primary = data.get("primaryData") or {}
    raw_price = str(primary.get("lastSalePrice") or "").replace("$", "").replace(",", "")
    price = float(raw_price)
    if price <= 0:
        raise ValueError("Nasdaq returned no valid price")
    observed_at = _parse_nasdaq_time(primary.get("lastTradeTimestamp"))
    if observed_at is None:
        raise ValueError("Nasdaq returned no valid quote timestamp")
    return UsEquityQuote(
        ticker=ticker,
        provider_symbol=ticker,
        price=price,
        observed_at=observed_at,
        source="Nasdaq delayed",
        session=str(data.get("marketStatus") or "closed").lower(),
        is_delayed=not bool(primary.get("isRealTime")),
    )


async def _quote_spec(
    client: httpx.AsyncClient,
    spec: UsSpotSpec,
) -> tuple[UsEquityQuote | None, str | None]:
    errors: list[str] = []
    for symbol in spec.quote_symbols:
        for fetcher in (_nasdaq_quote, _yahoo_quote):
            try:
                async with asyncio.timeout(12):
                    quote = await fetcher(client, symbol)
                return UsEquityQuote(
                    ticker=spec.ticker,
                    provider_symbol=symbol,
                    price=quote.price,
                    observed_at=quote.observed_at,
                    source=quote.source,
                    session=quote.session,
                    is_delayed=quote.is_delayed or symbol != spec.ticker,
                ), None
            except Exception as exc:
                errors.append(f"{symbol}/{fetcher.__name__}: {type(exc).__name__}: {exc}")
    return None, "; ".join(errors)[-500:]


async def collect_us_equity_quotes(
    client: httpx.AsyncClient,
    underlyings: set[str],
    *,
    concurrency: int = 6,
) -> UsEquityCollection:
    specs_by_ticker: dict[str, UsSpotSpec] = {}
    for underlying in underlyings:
        spec = get_us_spot_spec(underlying)
        if spec is not None:
            specs_by_ticker[spec.ticker] = spec

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def fetch(spec: UsSpotSpec) -> tuple[UsSpotSpec, UsEquityQuote | None, str | None]:
        async with semaphore:
            quote, error = await _quote_spec(client, spec)
        return spec, quote, error

    outcomes = await asyncio.gather(*(fetch(spec) for spec in specs_by_ticker.values()))
    instruments: list[Instrument] = []
    snapshots: list[MarketSnapshot] = []
    errors: list[str] = []
    for spec, quote, error in outcomes:
        metadata: dict[str, object] = {
            "quote_source": quote.source if quote else None,
            "provider_symbol": quote.provider_symbol if quote else spec.quote_symbols[0],
            "quote_session": quote.session if quote else "unavailable",
            "quote_delayed": quote.is_delayed if quote else True,
        }
        instruments.append(
            Instrument(
                venue=US_EQUITY_VENUE,
                symbol=spec.ticker,
                underlying=spec.ticker,
                display_name=spec.ticker,
                product_type="stock",
                metadata=metadata,
            )
        )
        if quote is not None:
            snapshots.append(
                MarketSnapshot(
                    venue=US_EQUITY_VENUE,
                    symbol=spec.ticker,
                    underlying=spec.ticker,
                    observed_at=quote.observed_at,
                    source_observed_at=quote.observed_at,
                    bid=None,
                    ask=None,
                    mark_price=quote.price,
                    index_price=quote.price,
                )
            )
        elif error:
            errors.append(f"{spec.ticker}: {error}")

    return UsEquityCollection(
        result=AdapterResult(instruments=instruments, snapshots=snapshots),
        errors=tuple(errors),
    )
