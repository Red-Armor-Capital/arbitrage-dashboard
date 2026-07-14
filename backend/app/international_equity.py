from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .models import AdapterResult, Instrument, MarketSnapshot
from .us_equity import EquityQuote, fetch_yahoo_quote

if TYPE_CHECKING:
    from .security_registry import SecuritySpec


DIRECT_FX_SYMBOLS = {
    "HKD": "HKD=X",
    "JPY": "JPY=X",
    "KRW": "KRW=X",
    "TWD": "TWD=X",
}


@dataclass(frozen=True)
class InternationalEquityCollection:
    result: AdapterResult
    errors: tuple[str, ...] = ()


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _fx_symbol(spec: SecuritySpec) -> str | None:
    if spec.local_currency.upper() == "USD":
        return None
    return spec.fx_symbol or DIRECT_FX_SYMBOLS.get(spec.local_currency.upper())


def _session_for_market(spec: SecuritySpec, quote: EquityQuote) -> str:
    """Correct Yahoo's continuous-session templates for exchange lunch breaks."""

    session = quote.session.lower()
    if session != "regular":
        return session
    try:
        local_time = quote.observed_at.astimezone(ZoneInfo(spec.timezone)).time()
    except ZoneInfoNotFoundError:
        return session

    market = spec.market.upper()
    if market in {"HK", "HKG", "HKEX"} and time(12) <= local_time < time(13):
        return "break"
    if market in {"JP", "JPN", "JPX"} and time(11, 30) <= local_time < time(12, 30):
        return "break"
    return session


async def _load_quote(
    client: httpx.AsyncClient,
    symbols: tuple[str, ...],
) -> tuple[EquityQuote | None, str | None]:
    errors: list[str] = []
    for symbol in symbols:
        try:
            async with asyncio.timeout(12):
                return await fetch_yahoo_quote(client, symbol), None
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
    return None, "; ".join(errors)[-500:]


def _source(stock: EquityQuote | None, fx: EquityQuote | None) -> str | None:
    if stock is None:
        return None
    if fx is None:
        return stock.source
    return f"{stock.source} · {fx.source} {fx.provider_symbol}"


def _metadata(
    spec: SecuritySpec,
    *,
    stock: EquityQuote | None,
    fx: EquityQuote | None,
    fx_symbol: str | None,
    quote_valid: bool,
) -> dict[str, object]:
    fx_rate = 1.0 if fx_symbol is None else (fx.price if fx else None)
    observed_at = (
        min(stock.observed_at, fx.observed_at)
        if stock is not None and fx is not None
        else stock.observed_at if stock is not None and fx_symbol is None else None
    )
    session = _session_for_market(spec, stock) if stock else "unavailable"
    delayed = bool(
        quote_valid
        and (
            stock is not None
            and stock.is_delayed
            or fx is not None
            and fx.is_delayed
            or stock is not None
            and stock.provider_symbol != spec.ticker
        )
    )
    source = _source(stock, fx)
    delay_status = (
        "unavailable"
        if not quote_valid
        else "delayed" if delayed else "realtime"
    )
    return {
        "security_id": spec.security_id,
        "mic": spec.mic,
        "market": spec.market,
        "ticker": spec.ticker,
        "local_currency": spec.local_currency,
        "local_price": stock.price if stock else None,
        "fx_symbol": fx_symbol,
        "fx_rate": fx_rate,
        "quote_valid": quote_valid,
        "delay_status": delay_status,
        "source": source,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "quote_session": session,
        "stock_observed_at": stock.observed_at.isoformat() if stock else None,
        "fx_observed_at": fx.observed_at.isoformat() if fx else None,
        # Compatibility with the existing spot analytics/storage metadata.
        "provider_symbol": (
            stock.provider_symbol
            if stock
            else (spec.quote_symbols[0] if spec.quote_symbols else spec.ticker)
        ),
        "quote_source": source,
        "quote_delayed": delayed if quote_valid else True,
        "spot_market": spec.market,
        "local_per_usd": fx_rate,
        "asset_class": spec.asset_class,
        "spot_carry_eligible": spec.asset_class in {"stock", "etf"},
    }


async def collect_international_equity_quotes(
    client: httpx.AsyncClient,
    securities: Iterable[SecuritySpec],
    *,
    concurrency: int = 6,
) -> InternationalEquityCollection:
    """Collect local-market equity quotes and normalize valid prices to USD.

    A failed stock or FX leg still emits an Instrument with ``quote_valid=false``
    but never emits a MarketSnapshot. Consumers can therefore invalidate any
    previously stored last-good snapshot instead of silently reusing it.
    """

    specs_by_id = {spec.security_id: spec for spec in securities}
    specs = list(specs_by_id.values())
    if not specs:
        return InternationalEquityCollection(result=AdapterResult())

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def load_stock(
        spec: SecuritySpec,
    ) -> tuple[SecuritySpec, EquityQuote | None, str | None]:
        symbols = spec.quote_symbols or (spec.ticker,)
        async with semaphore:
            quote, error = await _load_quote(client, symbols)
        return spec, quote, error

    fx_symbols = sorted(
        {
            symbol
            for spec in specs
            if spec.local_currency.upper() != "USD"
            if (symbol := _fx_symbol(spec)) is not None
        }
    )

    async def load_fx(
        symbol: str,
    ) -> tuple[str, EquityQuote | None, str | None]:
        async with semaphore:
            quote, error = await _load_quote(client, (symbol,))
        return symbol, quote, error

    stock_results, fx_results = await asyncio.gather(
        asyncio.gather(*(load_stock(spec) for spec in specs)),
        asyncio.gather(*(load_fx(symbol) for symbol in fx_symbols)),
    )
    fx_by_symbol = {symbol: quote for symbol, quote, _error in fx_results}

    errors = [
        f"FX {symbol}: {error}"
        for symbol, quote, error in fx_results
        if quote is None and error
    ]
    instruments: list[Instrument] = []
    snapshots: list[MarketSnapshot] = []

    for spec, stock, stock_error in stock_results:
        fx_symbol = _fx_symbol(spec)
        fx = fx_by_symbol.get(fx_symbol) if fx_symbol else None
        supported_fx = spec.local_currency.upper() == "USD" or fx_symbol is not None
        fx_valid = spec.local_currency.upper() == "USD" or (
            fx is not None and _positive(fx.price)
        )
        quote_valid = bool(
            supported_fx and stock is not None and _positive(stock.price) and fx_valid
        )
        metadata = _metadata(
            spec,
            stock=stock,
            fx=fx,
            fx_symbol=fx_symbol,
            quote_valid=quote_valid,
        )
        instruments.append(
            Instrument(
                venue=spec.spot_venue,
                symbol=spec.ticker,
                underlying=spec.underlying,
                display_name=spec.display_name,
                product_type="stock",
                quote_currency="USD",
                metadata=metadata,
            )
        )

        if stock is None and stock_error:
            errors.append(f"{spec.security_id}: {stock_error}")
        elif stock is not None and not _positive(stock.price):
            errors.append(f"{spec.security_id}: invalid non-positive stock quote")
        if not supported_fx:
            errors.append(
                f"{spec.security_id}: no USD conversion configured for "
                f"{spec.local_currency}"
            )
        elif fx_symbol is not None and fx is not None and not _positive(fx.price):
            errors.append(f"FX {fx_symbol}: invalid non-positive rate")
        if not quote_valid:
            continue

        fx_rate = 1.0 if fx_symbol is None else float(fx.price)
        observed_at = stock.observed_at if fx is None else min(
            stock.observed_at, fx.observed_at
        )
        usd_price = stock.price / fx_rate
        snapshots.append(
            MarketSnapshot(
                venue=spec.spot_venue,
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

    return InternationalEquityCollection(
        result=AdapterResult(instruments=instruments, snapshots=snapshots),
        errors=tuple(errors),
    )
