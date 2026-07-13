from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import VenueAdapter
from ..asset_registry import (
    asset_metadata,
    resolve_asset,
    resolve_hotstuff_asset,
    resolve_lighter_asset,
    resolve_orderly_asset,
)
from ..models import (
    AdapterResult,
    FundingRate,
    HistoryBatchResult,
    HistoryFetchOutcome,
    Instrument,
    MarketSnapshot,
    utc_now,
)


def _millis(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _seconds(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_datetime(value: object) -> datetime | None:
    """Parse an optional venue timestamp without inventing a source time."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return _iso_datetime(value)
    return _millis(numeric) if abs(numeric) >= 100_000_000_000 else _seconds(numeric)


def _hour_floor(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "data", "result", "rows", "markets", "order_books", "orderBooks",
        "order_book_details", "funding_rates", "fundings", "perps",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _rows(value)
            if nested:
                return nested
    return []


def _truthy(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "enabled"}


def _strict_next_utc_hour(observed_at: datetime | None = None) -> datetime:
    """Return the next UTC hour boundary, never the current boundary."""

    observed = (observed_at or utc_now()).astimezone(timezone.utc)
    return _hour_floor(observed) + timedelta(hours=1)


def _future_target(
    candidate: datetime | None,
    observed_at: datetime,
) -> datetime | None:
    """Accept an API target only when it is strictly in the future."""

    if candidate is None:
        return None
    normalized = candidate.astimezone(timezone.utc)
    return normalized if normalized > observed_at.astimezone(timezone.utc) else None


_SUPPORTED_FUNDING_INTERVALS = (1.0, 2.0, 4.0, 8.0)
_INTERVAL_TOLERANCE_HOURS = 5 / 60
_IDENTITY_TRANSFORM = "identity-v1"
_PERCENT_TO_DECIMAL_TRANSFORM = "percent-to-decimal-v1"
_EIGHT_HOUR_TO_HOURLY_TRANSFORM = "eight-hour-to-hourly-v1"


def _canonical_funding_interval(interval_hours: float) -> float | None:
    if not math.isfinite(interval_hours) or interval_hours <= 0:
        return None
    for supported in _SUPPORTED_FUNDING_INTERVALS:
        if math.isclose(
            interval_hours,
            supported,
            rel_tol=0,
            abs_tol=_INTERVAL_TOLERANCE_HOURS,
        ):
            return supported
    return None


def _next_utc_funding_boundary(
    observed_at: datetime,
    interval_hours: float,
) -> datetime:
    """Derive a strict UTC settlement boundary for standard funding tenors."""

    interval = _canonical_funding_interval(interval_hours)
    if interval is None:
        return _strict_next_utc_hour(observed_at)
    period_seconds = int(interval * 3600)
    timestamp = observed_at.astimezone(timezone.utc).timestamp()
    next_timestamp = (math.floor(timestamp / period_seconds) + 1) * period_seconds
    return datetime.fromtimestamp(next_timestamp, tz=timezone.utc)


async def _gather_limited(
    items: list[Instrument],
    loader: Callable[[Instrument], Awaitable[list[FundingRate]]],
    limit: int = 8,
) -> list[FundingRate]:
    """Fetch per-market history without sending an unbounded request burst."""

    semaphore = asyncio.Semaphore(limit)

    async def run(item: Instrument) -> list[FundingRate]:
        async with semaphore:
            return await loader(item)

    batches = await asyncio.gather(*(run(item) for item in items), return_exceptions=True)
    result: list[FundingRate] = []
    for batch in batches:
        if isinstance(batch, list):
            result.extend(batch)
    return result


def _extended_base(item: dict[str, Any]) -> str:
    value = str(item.get("uiName") or item.get("ui_name") or item.get("name") or "")
    normalized = value.strip().upper().replace(" ", "")
    for separator in ("-", "/"):
        if separator in normalized:
            normalized = normalized.split(separator, 1)[0]
    return normalized


def _stock_category_names(payload: Any) -> set[str]:
    if not isinstance(payload, list):
        return set()
    result: set[str] = set()
    for item in payload:
        if isinstance(item, list) and len(item) >= 2:
            coin, category = item[0], item[1]
        elif isinstance(item, dict):
            coin = item.get("coin") or item.get("name") or item.get("symbol")
            category = item.get("category") or item.get("type")
        else:
            continue
        if coin and str(category).strip().lower() in {"stock", "stocks"}:
            result.add(str(coin))
    return result


class LighterAdapter(VenueAdapter):
    venue = "lighter"
    history_mode = "per_symbol"
    base_url = "https://mainnet.zklighter.elliot.ai"
    maker_fee = 0.0
    taker_fee = 0.0

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        books_response, funding_response, details_response = await asyncio.gather(
            self.client.get(f"{self.base_url}/api/v1/orderBooks"),
            self.client.get(f"{self.base_url}/api/v1/funding-rates"),
            self.client.get(f"{self.base_url}/api/v1/orderBookDetails"),
        )
        books_response.raise_for_status()
        funding_response.raise_for_status()
        details_response.raise_for_status()
        funding_rows = _rows(funding_response.json())
        funding_by_market: dict[str, dict[str, Any]] = {}
        for row in funding_rows:
            if str(row.get("exchange") or "lighter").lower() != "lighter":
                continue
            key = str(row.get("market_id") or row.get("marketId") or row.get("symbol") or "")
            funding_by_market[key] = row

        details_by_market: dict[str, dict[str, Any]] = {}
        for row in _rows(details_response.json()):
            for key in (
                row.get("market_id"),
                row.get("marketId"),
                row.get("symbol"),
            ):
                if key not in (None, ""):
                    details_by_market[str(key)] = row

        instruments: list[Instrument] = []
        detail_by_symbol: dict[str, dict[str, Any]] = {}
        for item in _rows(books_response.json()):
            symbol = str(item.get("symbol") or item.get("market") or item.get("name") or "")
            spec = resolve_lighter_asset(symbol)
            if spec is None:
                continue
            status = str(item.get("status") or "active").lower()
            if status not in ("active", "trading", "open"):
                continue
            market_id = item.get("market_id") or item.get("marketId") or item.get("id")
            detail = details_by_market.get(str(market_id)) or details_by_market.get(symbol) or {}
            instrument = Instrument(
                venue=self.venue,
                symbol=symbol,
                underlying=spec.underlying,
                display_name=spec.display_name,
                quote_currency=str(item.get("quote_symbol") or item.get("quote") or "USDC"),
                funding_interval_hours=1,
                maker_fee=self.maker_fee,
                taker_fee=self.taker_fee,
                metadata={
                    "market_id": market_id,
                    "funding_model": "discrete_snapshot",
                    "fee_tier": "standard",
                    "base_interest_rate": item.get("base_interest_rate"),
                    "funding_clamp_small": item.get("funding_clamp_small"),
                    "funding_clamp_big": item.get("funding_clamp_big"),
                    "market_type": item.get("market_type"),
                    "strategy_index": detail.get("strategy_index"),
                    "status": status,
                    **asset_metadata(spec, symbol),
                },
            )
            instruments.append(instrument)
            detail_by_symbol[symbol] = detail

        snapshots: list[MarketSnapshot] = []
        current: list[FundingRate] = []
        collection_observed = utc_now()
        for instrument in instruments:
            detail = detail_by_symbol.get(instrument.symbol, {})
            market_id = str(instrument.metadata.get("market_id") or "")
            funding_item = funding_by_market.get(market_id) or funding_by_market.get(instrument.symbol) or {}
            detail_rate_value = (
                detail.get("current_funding_rate")
                if "current_funding_rate" in detail
                else detail.get("currentFundingRate")
            )
            detail_rate = self.as_float(detail_rate_value)
            normalized_rate_value = (
                funding_item.get("rate")
                if "rate" in funding_item
                else funding_item.get("funding_rate")
            )
            normalized_eight_hour_rate = self.as_float(normalized_rate_value)
            if detail_rate is not None:
                rate = detail_rate / 100
                raw_rate = detail_rate
                raw_rate_unit = "percent"
                source_tenor_hours = 1.0
                transform_version = _PERCENT_TO_DECIMAL_TRANSFORM
            elif normalized_eight_hour_rate is not None:
                rate = normalized_eight_hour_rate / 8
                raw_rate = normalized_eight_hour_rate
                raw_rate_unit = "decimal"
                source_tenor_hours = 8.0
                transform_version = _EIGHT_HOUR_TO_HOURLY_TRANSFORM
            else:
                rate = None
                raw_rate = None
                raw_rate_unit = "decimal"
                source_tenor_hours = None
                transform_version = _IDENTITY_TRANSFORM
            # Lighter's funding_timestamp is the last settled round, not the
            # target of current_funding_rate. Current estimates settle on the
            # next strict UTC hour.
            next_funding = _strict_next_utc_hour(collection_observed)
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=collection_observed,
                    bid=self.as_float(detail.get("best_bid_price") or detail.get("best_bid")),
                    ask=self.as_float(detail.get("best_ask_price") or detail.get("best_ask")),
                    mark_price=self.as_float(
                        detail.get("mark_price") or detail.get("last_trade_price")
                    ),
                    index_price=self.as_float(detail.get("index_price")),
                    funding_rate=rate,
                    funding_interval_hours=1,
                    next_funding_at=next_funding,
                    source_observed_at=_source_datetime(detail.get("timestamp")),
                    target_source="schedule",
                    raw_funding_rate=raw_rate,
                    raw_rate_unit=raw_rate_unit,
                    source_tenor_hours=source_tenor_hours,
                    transform_version=transform_version,
                    open_interest=self.as_float(detail.get("open_interest")),
                    volume_24h=self.as_float(detail.get("daily_quote_token_volume")),
                )
            )
            if rate is not None:
                current.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        observed_at=collection_observed,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=1,
                        kind="current",
                    )
                )

        history: list[FundingRate] = []
        if include_history:
            eligible = [
                item
                for item in instruments
                if item.metadata.get("spot_carry_eligible") is True
            ]
            history = await _gather_limited(
                eligible,
                lambda item: self._history(item, history_since),
            )
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/api/v1/fundings",
            params={
                "market_id": instrument.metadata.get("market_id"),
                "resolution": "1h",
                "start_timestamp": int(since.timestamp()),
                "end_timestamp": int(utc_now().timestamp()),
                "count_back": 750,
            },
        )
        response.raise_for_status()
        observed = utc_now()
        result: list[FundingRate] = []
        for item in _rows(response.json()):
            effective = _seconds(item.get("timestamp"))
            rate_percent = self.as_float(item.get("rate"))
            if not effective or rate_percent is None:
                continue
            direction = str(item.get("direction") or "long").lower()
            sign = 1 if direction == "long" else -1
            result.append(
                FundingRate(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=observed,
                    effective_at=effective,
                    rate=sign * rate_percent / 100,
                    interval_hours=1,
                    kind="settled",
                )
            )
        return result


class ExtendedAdapter(VenueAdapter):
    venue = "extended"
    history_mode = "per_symbol"
    base_url = "https://api.starknet.extended.exchange"
    maker_fee = 0.0
    taker_fee = 0.00025

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        response = await self.client.get(f"{self.base_url}/api/v1/info/markets")
        response.raise_for_status()
        instruments: list[Instrument] = []
        stats_by_symbol: dict[str, dict[str, Any]] = {}
        for item in _rows(response.json()):
            if str(item.get("category") or "").lower() != "tradfi":
                continue
            if str(item.get("subCategory") or item.get("sub_category") or "").lower() != "equity":
                continue
            if item.get("visibleOnUi") is False:
                continue
            if str(item.get("status") or "active").lower() not in ("active", "trading", "open"):
                continue
            name = str(item.get("name") or "")
            raw_underlying = _extended_base(item)
            if not name or not raw_underlying:
                continue
            spec = resolve_asset(raw_underlying)
            stats = item.get("marketStats") or item.get("market_stats") or {}
            instrument = Instrument(
                venue=self.venue,
                symbol=name,
                underlying=spec.underlying,
                display_name=spec.display_name,
                quote_currency="USDC",
                funding_interval_hours=1,
                maker_fee=self.maker_fee,
                taker_fee=self.taker_fee,
                metadata={
                    "funding_model": "discrete_snapshot",
                    "hourly_funding_cap": item.get("hourlyFundingRateCap"),
                    "category": item.get("category"),
                    "sub_category": item.get("subCategory") or item.get("sub_category"),
                    "status": item.get("status"),
                    "visible_on_ui": item.get("visibleOnUi"),
                    "market_type": item.get("type"),
                    **asset_metadata(spec, raw_underlying),
                },
            )
            instruments.append(instrument)
            stats_by_symbol[name] = stats

        snapshots: list[MarketSnapshot] = []
        current: list[FundingRate] = []
        collection_observed = utc_now()
        for instrument in instruments:
            stats = stats_by_symbol.get(instrument.symbol, {})
            rate_value = (
                stats.get("fundingRate")
                if "fundingRate" in stats
                else stats.get("funding_rate")
            )
            rate = self.as_float(rate_value)
            target_value = (
                stats.get("nextFundingTime")
                if "nextFundingTime" in stats
                else stats.get("nextFundingRate")
            )
            if target_value in (None, "", 0, "0"):
                target_value = stats.get("fundingTimestamp")
            api_target = _future_target(_millis(target_value), collection_observed)
            if api_target is not None:
                next_funding = api_target
                target_source = "api"
            else:
                next_funding = _strict_next_utc_hour(collection_observed)
                target_source = "schedule"
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=collection_observed,
                    bid=self.as_float(stats.get("bestBid") or stats.get("bidPrice")),
                    ask=self.as_float(stats.get("bestAsk") or stats.get("askPrice")),
                    mark_price=self.as_float(stats.get("markPrice")),
                    index_price=self.as_float(stats.get("indexPrice")),
                    funding_rate=rate,
                    funding_interval_hours=1,
                    next_funding_at=next_funding,
                    source_observed_at=_source_datetime(
                        stats.get("updatedTime") or stats.get("timestamp")
                    ),
                    target_source=target_source,
                    raw_funding_rate=rate,
                    raw_rate_unit="decimal",
                    source_tenor_hours=1,
                    transform_version=_IDENTITY_TRANSFORM,
                    open_interest=self.as_float(stats.get("openInterest")),
                    volume_24h=self.as_float(stats.get("dailyVolume") or stats.get("volume24h")),
                )
            )
            if rate is not None:
                current.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        observed_at=collection_observed,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=1,
                        kind="current",
                    )
                )
        history: list[FundingRate] = []
        if include_history:
            history = await _gather_limited(
                instruments,
                lambda item: self._history(item, history_since),
            )
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/api/v1/info/{instrument.symbol}/funding",
            params={
                "startTime": int(since.timestamp() * 1000),
                "endTime": int(utc_now().timestamp() * 1000),
                "limit": 10_000,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Extended funding history schema")
        status = str(payload.get("status") or "OK").upper()
        if status != "OK":
            raise RuntimeError(
                f"Extended funding history error: {payload.get('error')}"
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected Extended funding history rows")
        observed = utc_now()
        result: list[FundingRate] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            effective = _millis(
                item.get("T") or item.get("timestamp") or item.get("fundingTime")
            )
            if "f" in item:
                rate_value = item.get("f")
            else:
                rate_value = item.get("fundingRate") or item.get("rate")
            rate = self.as_float(rate_value)
            if not effective or rate is None:
                continue
            result.append(
                FundingRate(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=observed,
                    effective_at=effective,
                    rate=rate,
                    interval_hours=1,
                    kind="settled",
                )
            )
        return result


class XyzAdapter(VenueAdapter):
    venue = "xyz"
    history_mode = "per_symbol"
    base_url = "https://api.hyperliquid.xyz/info"

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        meta_response, category_response = await asyncio.gather(
            self.client.post(self.base_url, json={"type": "metaAndAssetCtxs", "dex": "xyz"}),
            self.client.post(self.base_url, json={"type": "perpCategories"}),
        )
        meta_response.raise_for_status()
        category_response.raise_for_status()
        payload = meta_response.json()
        if not isinstance(payload, list) or len(payload) < 2:
            raise RuntimeError("Unexpected Hyperliquid metaAndAssetCtxs response")
        meta, contexts = payload[0], payload[1]
        if not isinstance(meta, dict) or not isinstance(contexts, list):
            raise RuntimeError("Unexpected Hyperliquid market metadata schema")
        stock_coins = _stock_category_names(category_response.json())
        if not stock_coins:
            raise RuntimeError("Hyperliquid stock category is empty; refusing to classify markets")
        instruments: list[Instrument] = []
        snapshots: list[MarketSnapshot] = []
        collection_observed = utc_now()
        next_funding = _strict_next_utc_hour(collection_observed)
        universe = meta.get("universe", [])
        if not isinstance(universe, list) or len(universe) != len(contexts):
            raise RuntimeError("Hyperliquid universe/context length mismatch")
        for item, ctx in zip(universe, contexts, strict=True):
            if not isinstance(item, dict) or not isinstance(ctx, dict):
                continue
            raw_name = str(item.get("name") or "")
            coin = raw_name if raw_name.startswith("xyz:") else f"xyz:{raw_name}"
            if item.get("isDelisted"):
                continue
            if coin not in stock_coins and raw_name not in stock_coins:
                continue
            raw_underlying = raw_name.split(":")[-1].upper()
            if not raw_underlying:
                continue
            spec = resolve_asset(raw_underlying)
            growth_value = item.get("growthMode")
            growth_mode = growth_value is True or str(growth_value).lower() == "enabled"
            maker_fee = 0.00003 if growth_mode else 0.0003
            taker_fee = 0.00009 if growth_mode else 0.0009
            instrument = Instrument(
                venue=self.venue,
                symbol=coin,
                underlying=spec.underlying,
                display_name=spec.display_name,
                quote_currency="USDC",
                funding_interval_hours=1,
                maker_fee=maker_fee,
                taker_fee=taker_fee,
                metadata={
                    "funding_model": "discrete_snapshot",
                    "growth_mode": growth_mode,
                    "max_leverage": item.get("maxLeverage"),
                    "margin_mode": item.get("marginMode"),
                    "official_category": "stocks",
                    "is_delisted": False,
                    **asset_metadata(spec, raw_underlying),
                },
            )
            instruments.append(instrument)
            impact = ctx.get("impactPxs") or []
            rate = self.as_float(ctx.get("funding"))
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=coin,
                    underlying=spec.underlying,
                    observed_at=collection_observed,
                    bid=self.as_float(impact[0]) if len(impact) > 0 else None,
                    ask=self.as_float(impact[1]) if len(impact) > 1 else None,
                    mark_price=self.as_float(ctx.get("markPx")),
                    index_price=self.as_float(ctx.get("oraclePx")),
                    funding_rate=rate,
                    funding_interval_hours=1,
                    next_funding_at=next_funding,
                    source_observed_at=None,
                    target_source="schedule",
                    raw_funding_rate=rate,
                    raw_rate_unit="decimal",
                    source_tenor_hours=1,
                    transform_version=_IDENTITY_TRANSFORM,
                    open_interest=self.as_float(ctx.get("openInterest")),
                    volume_24h=self.as_float(ctx.get("dayNtlVlm")),
                )
            )

        current = [
            FundingRate(
                venue=self.venue,
                symbol=item.symbol,
                underlying=item.underlying,
                observed_at=collection_observed,
                effective_at=next_funding,
                rate=snapshot.funding_rate,
                interval_hours=1,
                kind="current",
            )
            for item, snapshot in zip(instruments, snapshots, strict=True)
            if snapshot.funding_rate is not None
        ]
        history: list[FundingRate] = []
        if include_history:
            history = await _gather_limited(
                instruments,
                lambda item: self._history(item, history_since),
            )
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.post(
            self.base_url,
            json={
                "type": "fundingHistory",
                "coin": instrument.symbol,
                "startTime": int(since.timestamp() * 1000),
                "endTime": int(utc_now().timestamp() * 1000),
            },
        )
        response.raise_for_status()
        observed = utc_now()
        return [
            FundingRate(
                venue=self.venue,
                symbol=instrument.symbol,
                underlying=instrument.underlying,
                observed_at=observed,
                effective_at=_millis(item.get("time")) or observed,
                rate=float(item["fundingRate"]),
                interval_hours=1,
                kind="settled",
            )
            for item in response.json()
            if item.get("fundingRate") is not None
        ]


class HotstuffAdapter(VenueAdapter):
    venue = "hotstuff"
    history_mode = "per_symbol"
    base_url = "https://api.hotstuff.trade/info"
    maker_fee = -0.00002
    taker_fee = 0.00025
    # This public address is an active liquidity provider observed across every
    # reviewed Hotstuff stock/ETF market. Funding rates are global, so one
    # account's exact payment records are sufficient after strict validation.
    history_observer = "0xA304C76172D833Da781C91513403ac6F45474b86"
    history_page_size = 500
    history_max_pages = 50

    def history_instruments(self, instruments: list[Instrument]) -> list[Instrument]:
        return [
            instrument
            for instrument in instruments
            if instrument.active
            and instrument.metadata.get("spot_carry_eligible") is True
            and instrument.metadata.get("instrument_id") is not None
        ]

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        instrument_response, ticker_response = await asyncio.gather(
            self.client.post(self.base_url, json={"method": "instruments", "params": {"type": "perps"}}),
            self.client.post(self.base_url, json={"method": "ticker", "params": {"symbol": "all"}}),
        )
        instrument_response.raise_for_status()
        ticker_response.raise_for_status()
        instruments: list[Instrument] = []
        for item in _rows(instrument_response.json()):
            symbol = str(item.get("symbol") or item.get("name") or "")
            status = str(item.get("status") or "active").lower()
            if (
                not symbol
                or _truthy(item.get("delisted"))
                or status not in {"active", "trading", "open"}
            ):
                continue
            price_index = item.get("price_index") or item.get("priceIndex")
            spec = resolve_hotstuff_asset(symbol, str(price_index or ""))
            if spec is None:
                continue
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=symbol,
                    underlying=spec.underlying,
                    display_name=spec.display_name,
                    quote_currency="USDC",
                    funding_interval_hours=1,
                    maker_fee=self.maker_fee,
                    taker_fee=self.taker_fee,
                    metadata={
                        "instrument_id": item.get("id"),
                        "funding_model": "discrete_snapshot",
                        "history_source": "public_account_funding_history",
                        "current_rate_tenor_hours": 1,
                        "settlement_interval_hours": 1,
                        "ticker_rate_semantics": "hourly_payment_rate",
                        "price_index": price_index,
                        "growth_mode": item.get("growth_mode") or item.get("growthMode"),
                        "delisted": False,
                        "status": status,
                        **asset_metadata(spec, str(price_index or symbol)),
                    },
                )
            )
        instrument_map = {item.symbol: item for item in instruments}
        snapshots: list[MarketSnapshot] = []
        current: list[FundingRate] = []
        collection_observed = utc_now()
        for item in _rows(ticker_response.json()):
            symbol = str(item.get("symbol") or item.get("s") or "")
            instrument = instrument_map.get(symbol)
            if not instrument:
                continue
            raw_rate_value = (
                item.get("funding_rate")
                if "funding_rate" in item
                else item.get("fundingRate")
            )
            raw_hourly_rate = self.as_float(raw_rate_value)
            # Hotstuff's product UI discusses an 8-hour display rate, but the
            # public ticker field already matches the hourly rate used by exact
            # funding-payment records. Treating it as an 8-hour value would
            # divide the actual payment rate twice.
            rate = raw_hourly_rate
            next_funding = _strict_next_utc_hour(collection_observed)
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=symbol,
                    underlying=instrument.underlying,
                    observed_at=collection_observed,
                    bid=self.as_float(item.get("best_bid") or item.get("bid")),
                    ask=self.as_float(item.get("best_ask") or item.get("ask")),
                    mark_price=self.as_float(item.get("mark_price") or item.get("markPrice")),
                    index_price=self.as_float(item.get("index_price") or item.get("indexPrice")),
                    funding_rate=rate,
                    funding_interval_hours=1,
                    next_funding_at=next_funding,
                    source_observed_at=_source_datetime(item.get("timestamp")),
                    target_source="schedule",
                    raw_funding_rate=raw_hourly_rate,
                    raw_rate_unit="decimal",
                    source_tenor_hours=1,
                    transform_version=_IDENTITY_TRANSFORM,
                    open_interest=self.as_float(item.get("open_interest")),
                    volume_24h=self.as_float(item.get("volume_24h")),
                )
            )
            if rate is not None:
                current.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=symbol,
                        underlying=instrument.underlying,
                        observed_at=collection_observed,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=1,
                        kind="current",
                    )
                )
        # Exact settled rates are fetched separately from public funding-payment
        # records. The ticker estimate above is never promoted to settled history.
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current)

    async def collect_history(
        self,
        instruments: list[Instrument],
        history_since: datetime,
    ) -> HistoryBatchResult:
        if not instruments:
            return HistoryBatchResult()

        by_id: dict[int, Instrument] = {}
        missing_id: list[Instrument] = []
        for instrument in instruments:
            try:
                by_id[int(instrument.metadata.get("instrument_id"))] = instrument
            except (TypeError, ValueError):
                missing_id.append(instrument)

        outcomes: list[HistoryFetchOutcome] = [
            HistoryFetchOutcome(
                instrument=instrument,
                success=False,
                error="missing Hotstuff instrument id",
            )
            for instrument in missing_id
        ]
        if not by_id:
            return HistoryBatchResult(outcomes=outcomes)

        observed = utc_now()
        grouped_rates: dict[tuple[int, datetime], list[float]] = {}
        first_total_count: int | None = None

        try:
            reached_cutoff = False
            for page in range(1, self.history_max_pages + 1):
                response = await self.client.post(
                    self.base_url,
                    json={
                        "method": "funding_history",
                        "params": {
                            "user": self.history_observer,
                            "page": page,
                            "limit": self.history_page_size,
                        },
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise RuntimeError("Unexpected Hotstuff funding history schema")

                total_count = payload.get("total_count")
                if not isinstance(total_count, int):
                    raise RuntimeError("Hotstuff funding history omitted total_count")
                if first_total_count is None:
                    first_total_count = total_count
                elif total_count != first_total_count:
                    raise RuntimeError("Hotstuff funding history changed during pagination")

                page_times: list[datetime] = []
                for row in payload["data"]:
                    if not isinstance(row, dict):
                        continue
                    effective_raw = _iso_datetime(row.get("timestamp"))
                    if effective_raw is None:
                        continue
                    page_times.append(effective_raw)
                    if effective_raw < history_since or effective_raw > observed + timedelta(minutes=2):
                        continue
                    effective_at = _hour_floor(effective_raw)
                    settlement_latency = (effective_raw - effective_at).total_seconds()
                    if settlement_latency < 0 or settlement_latency > 120:
                        continue
                    try:
                        instrument_id = int(row.get("instrument_id"))
                    except (TypeError, ValueError):
                        continue
                    if instrument_id not in by_id:
                        continue

                    rate = self.as_float(row.get("funding_rate"))
                    payment = self.as_float(row.get("funding_payment"))
                    size = self.as_float(row.get("size"))
                    mark_price = self.as_float(row.get("mark_price"))
                    values = (rate, payment, size, mark_price)
                    if any(value is None or not math.isfinite(value) for value in values):
                        continue
                    assert rate is not None and payment is not None
                    assert size is not None and mark_price is not None
                    expected_payment = abs(size * mark_price * rate)
                    if not math.isclose(
                        abs(payment),
                        expected_payment,
                        rel_tol=1e-4,
                        abs_tol=2e-6,
                    ):
                        continue
                    grouped_rates.setdefault((instrument_id, effective_at), []).append(rate)

                if page_times and min(page_times) <= history_since:
                    reached_cutoff = True
                    break
                has_next = payload.get("has_next") is True
                total_pages = payload.get("total_pages")
                if not has_next or (isinstance(total_pages, int) and page >= total_pages):
                    reached_cutoff = True
                    break
            if not reached_cutoff:
                raise RuntimeError("Hotstuff funding history exceeded pagination guard")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:240]
            outcomes.extend(
                HistoryFetchOutcome(
                    instrument=instrument,
                    success=False,
                    error=error,
                )
                for instrument in by_id.values()
            )
            return HistoryBatchResult(outcomes=outcomes)

        funding_by_symbol: dict[str, list[FundingRate]] = {
            instrument.symbol: [] for instrument in by_id.values()
        }
        conflicted_symbols: set[str] = set()
        for (instrument_id, effective_at), rates in grouped_rates.items():
            instrument = by_id[instrument_id]
            if max(rates) - min(rates) > 1e-12:
                conflicted_symbols.add(instrument.symbol)
                continue
            funding_by_symbol[instrument.symbol].append(
                FundingRate(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=observed,
                    effective_at=effective_at,
                    rate=rates[0],
                    interval_hours=1,
                    kind="settled",
                )
            )

        for instrument in by_id.values():
            funding = sorted(
                funding_by_symbol[instrument.symbol],
                key=lambda item: item.effective_at,
            )
            if instrument.symbol in conflicted_symbols:
                outcomes.append(
                    HistoryFetchOutcome(
                        instrument=instrument,
                        success=False,
                        funding=funding,
                        error="conflicting settled funding rates",
                    )
                )
            elif not funding:
                outcomes.append(
                    HistoryFetchOutcome(
                        instrument=instrument,
                        success=False,
                        error="public observer has no validated settled funding rows",
                    )
                )
            else:
                outcomes.append(
                    HistoryFetchOutcome(
                        instrument=instrument,
                        success=True,
                        funding=funding,
                    )
                )
        return HistoryBatchResult(outcomes=outcomes)


class OrderlyAdapter(VenueAdapter):
    venue = "orderly"
    history_mode = "per_symbol"
    history_concurrency = 2
    base_url = "https://api.orderly.org"
    maker_fee = 0.0
    taker_fee = 0.0005

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        info_response, futures_response, funding_response = await asyncio.gather(
            self.client.get(f"{self.base_url}/v1/public/info"),
            self.client.get(f"{self.base_url}/v1/public/futures"),
            self.client.get(f"{self.base_url}/v1/public/funding_rates"),
        )
        for response in (info_response, futures_response, funding_response):
            response.raise_for_status()
        instruments: list[Instrument] = []
        for item in _rows(info_response.json()):
            symbol = str(item.get("symbol") or "")
            if not symbol or str(item.get("status") or "active").lower() not in {
                "active",
                "trading",
                "open",
            }:
                continue
            display_symbol_name = item.get("display_symbol_name") or item.get("displaySymbolName")
            broker_id = item.get("broker_id") or item.get("brokerId")
            spec = resolve_orderly_asset(
                symbol,
                str(display_symbol_name) if display_symbol_name else None,
                str(broker_id) if broker_id else None,
            )
            if spec is None:
                continue
            period = self.as_float(item.get("funding_period")) or 8
            if period > 1000:
                period /= 3_600_000
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=symbol,
                    underlying=spec.underlying,
                    display_name=spec.display_name,
                    quote_currency="USDC",
                    funding_interval_hours=period,
                    maker_fee=self.maker_fee,
                    taker_fee=self.taker_fee,
                    metadata={
                        "funding_model": "discrete_snapshot",
                        "cap_funding": item.get("cap_funding"),
                        "floor_funding": item.get("floor_funding"),
                        "interest_rate": item.get("interest_rate"),
                        "fee_source": "orderly_base_fee",
                        "broker_id": broker_id,
                        "status": item.get("status"),
                        **asset_metadata(spec, str(display_symbol_name or symbol)),
                    },
                )
            )
        instrument_map = {item.symbol: item for item in instruments}
        futures_map = {
            str(item.get("symbol")): item for item in _rows(futures_response.json())
        }
        funding_map = {
            str(item.get("symbol")): item for item in _rows(funding_response.json())
        }
        snapshots: list[MarketSnapshot] = []
        current: list[FundingRate] = []
        collection_observed = utc_now()
        for symbol, instrument in instrument_map.items():
            market = futures_map.get(symbol, {})
            funding = funding_map.get(symbol, {})
            rate_value = (
                funding.get("est_funding_rate")
                if "est_funding_rate" in funding
                else funding.get("estFundingRate")
            )
            if rate_value is None:
                rate_value = market.get("est_funding_rate")
            rate = self.as_float(rate_value)
            target_value = (
                funding.get("next_funding_time")
                if "next_funding_time" in funding
                else funding.get("nextFundingTime")
            )
            api_target = _future_target(_millis(target_value), collection_observed)
            if api_target is not None:
                next_funding = api_target
                target_source = "api"
            else:
                next_funding = _next_utc_funding_boundary(
                    collection_observed,
                    instrument.funding_interval_hours,
                )
                target_source = "schedule"
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=symbol,
                    underlying=instrument.underlying,
                    observed_at=collection_observed,
                    bid=self.as_float(market.get("bid")),
                    ask=self.as_float(market.get("ask")),
                    mark_price=self.as_float(market.get("mark_price")),
                    index_price=self.as_float(market.get("index_price")),
                    funding_rate=rate,
                    funding_interval_hours=instrument.funding_interval_hours,
                    next_funding_at=next_funding,
                    source_observed_at=_source_datetime(
                        funding.get("updated_time") or funding.get("timestamp")
                    ),
                    target_source=target_source,
                    raw_funding_rate=rate,
                    raw_rate_unit="decimal",
                    source_tenor_hours=instrument.funding_interval_hours,
                    transform_version=_IDENTITY_TRANSFORM,
                    open_interest=self.as_float(market.get("open_interest")),
                    volume_24h=self.as_float(market.get("24h_amount") or market.get("volume_24h")),
                )
            )
            if rate is not None:
                current.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=symbol,
                        underlying=instrument.underlying,
                        observed_at=collection_observed,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=instrument.funding_interval_hours,
                        kind="current",
                    )
                )
        history: list[FundingRate] = []
        if include_history:
            history = await _gather_limited(
                instruments,
                lambda item: self._history(item, history_since),
            )
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        # Include up to one maximum supported settlement interval before the
        # requested window so the first in-window row has a predecessor from
        # which its actual tenor can be inferred.
        request_since = since - timedelta(hours=max(_SUPPORTED_FUNDING_INTERVALS))
        response = await self.client.get(
            f"{self.base_url}/v1/public/funding_rate_history",
            params={
                "symbol": instrument.symbol,
                "start_t": int(request_since.timestamp() * 1000),
                "end_t": int(utc_now().timestamp() * 1000),
                "page": 1,
                "size": 500,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError(
                f"Orderly funding history error: {payload.get('message') if isinstance(payload, dict) else 'invalid response'}"
            )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise RuntimeError("Unexpected Orderly funding history schema")
        observed = utc_now()
        timeline: list[tuple[datetime, float | None]] = []
        for item in data["rows"]:
            if not isinstance(item, dict):
                continue
            effective_value = (
                item.get("funding_rate_timestamp")
                if "funding_rate_timestamp" in item
                else item.get("fundingTime")
            )
            rate_value = (
                item.get("funding_rate")
                if "funding_rate" in item
                else item.get("fundingRate")
            )
            effective = _millis(effective_value)
            if effective is None:
                continue
            timeline.append((effective, self.as_float(rate_value)))
        timeline.sort(key=lambda row: row[0])

        result: list[FundingRate] = []
        previous_effective: datetime | None = None
        for effective, rate in timeline:
            if previous_effective is None:
                previous_effective = effective
                continue
            elapsed_hours = (effective - previous_effective).total_seconds() / 3600
            previous_effective = effective
            interval_hours = _canonical_funding_interval(elapsed_hours)
            if (
                effective < since
                or rate is None
                or not math.isfinite(rate)
                or interval_hours is None
            ):
                continue
            result.append(
                FundingRate(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=observed,
                    effective_at=effective,
                    rate=rate,
                    interval_hours=interval_hours,
                    kind="settled",
                )
            )
        return result
