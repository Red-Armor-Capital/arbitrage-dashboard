from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .base import VenueAdapter
from ..models import AdapterResult, FundingRate, Instrument, MarketSnapshot, utc_now


def _from_millis(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _from_seconds(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class BinanceAdapter(VenueAdapter):
    venue = "binance"
    base_url = "https://fapi.binance.com"
    maker_fee = 0.0002
    taker_fee = 0.0005

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        info_response, premium_response, book_response, funding_info_response = await asyncio.gather(
            self.client.get(f"{self.base_url}/fapi/v1/exchangeInfo"),
            self.client.get(f"{self.base_url}/fapi/v1/premiumIndex"),
            self.client.get(f"{self.base_url}/fapi/v1/ticker/bookTicker"),
            self.client.get(f"{self.base_url}/fapi/v1/fundingInfo"),
        )
        for response in (info_response, premium_response, book_response, funding_info_response):
            response.raise_for_status()

        interval_by_symbol = {
            item["symbol"]: float(item.get("fundingIntervalHours") or 8)
            for item in funding_info_response.json()
        }
        instruments: list[Instrument] = []
        for item in info_response.json().get("symbols", []):
            underlying = self.match_underlying(item.get("symbol", ""))
            if not underlying:
                continue
            if item.get("contractType") != "TRADIFI_PERPETUAL" or item.get("status") != "TRADING":
                continue
            interval = interval_by_symbol.get(item["symbol"], 8.0)
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=item["symbol"],
                    underlying=underlying,
                    display_name=underlying,
                    quote_currency=item.get("quoteAsset", "USDT"),
                    funding_interval_hours=interval,
                    maker_fee=self.maker_fee,
                    taker_fee=self.taker_fee,
                    metadata={
                        "underlying_type": item.get("underlyingType"),
                        "underlying_subtype": item.get("underlyingSubType"),
                    },
                )
            )

        instrument_map = {item.symbol: item for item in instruments}
        books = {item["symbol"]: item for item in book_response.json()}
        snapshots: list[MarketSnapshot] = []
        current_funding: list[FundingRate] = []
        for item in premium_response.json():
            instrument = instrument_map.get(item.get("symbol"))
            if not instrument:
                continue
            book = books.get(instrument.symbol, {})
            next_funding = _from_millis(item.get("nextFundingTime"))
            rate = self.as_float(item.get("lastFundingRate"))
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    bid=self.as_float(book.get("bidPrice")),
                    ask=self.as_float(book.get("askPrice")),
                    mark_price=self.as_float(item.get("markPrice")),
                    index_price=self.as_float(item.get("indexPrice")),
                    funding_rate=rate,
                    funding_interval_hours=instrument.funding_interval_hours,
                    next_funding_at=next_funding,
                )
            )
            if rate is not None and next_funding is not None:
                current_funding.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=instrument.funding_interval_hours,
                        kind="current",
                    )
                )

        history = []
        if include_history:
            history_batches = await asyncio.gather(
                *(
                    self._history(item, history_since)
                    for item in instruments
                ),
                return_exceptions=True,
            )
            for batch in history_batches:
                if isinstance(batch, list):
                    history.extend(batch)
        return AdapterResult(
            instruments=instruments,
            snapshots=snapshots,
            funding=current_funding + history,
        )

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/fapi/v1/fundingRate",
            params={
                "symbol": instrument.symbol,
                "startTime": int(since.timestamp() * 1000),
                "limit": 1000,
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
                effective_at=_from_millis(item["fundingTime"]) or observed,
                rate=float(item["fundingRate"]),
                interval_hours=instrument.funding_interval_hours,
                kind="settled",
            )
            for item in response.json()
        ]


class BybitAdapter(VenueAdapter):
    venue = "bybit"
    base_url = "https://api.bybit.com"
    maker_fee = 0.0
    taker_fee = 0.000275

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        info_response, ticker_response = await asyncio.gather(
            self.client.get(
                f"{self.base_url}/v5/market/instruments-info",
                params={
                    "category": "linear",
                    "symbolType": "stock",
                    "status": "Trading",
                    "limit": 1000,
                },
            ),
            self.client.get(
                f"{self.base_url}/v5/market/tickers",
                params={"category": "linear"},
            ),
        )
        info_response.raise_for_status()
        ticker_response.raise_for_status()
        info_payload = info_response.json()
        ticker_payload = ticker_response.json()
        if info_payload.get("retCode") != 0:
            raise RuntimeError(info_payload.get("retMsg", "Bybit instruments failed"))
        if ticker_payload.get("retCode") != 0:
            raise RuntimeError(ticker_payload.get("retMsg", "Bybit tickers failed"))

        instruments: list[Instrument] = []
        for item in info_payload.get("result", {}).get("list", []):
            underlying = self.match_underlying(item.get("symbol", ""))
            if not underlying or item.get("status") != "Trading":
                continue
            interval = float(item.get("fundingInterval") or 480) / 60
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=item["symbol"],
                    underlying=underlying,
                    display_name=underlying,
                    quote_currency=item.get("quoteCoin", "USDT"),
                    funding_interval_hours=interval,
                    maker_fee=self.maker_fee,
                    taker_fee=self.taker_fee,
                    metadata={
                        "symbol_type": item.get("symbolType"),
                        "contract_type": item.get("contractType"),
                        "upper_funding_rate": item.get("upperFundingRate"),
                        "lower_funding_rate": item.get("lowerFundingRate"),
                    },
                )
            )

        instrument_map = {item.symbol: item for item in instruments}
        snapshots: list[MarketSnapshot] = []
        current_funding: list[FundingRate] = []
        for item in ticker_payload.get("result", {}).get("list", []):
            instrument = instrument_map.get(item.get("symbol"))
            if not instrument:
                continue
            rate = self.as_float(item.get("fundingRate"))
            next_funding = _from_millis(item.get("nextFundingTime"))
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    bid=self.as_float(item.get("bid1Price")),
                    ask=self.as_float(item.get("ask1Price")),
                    mark_price=self.as_float(item.get("markPrice")),
                    index_price=self.as_float(item.get("indexPrice")),
                    funding_rate=rate,
                    funding_interval_hours=instrument.funding_interval_hours,
                    next_funding_at=next_funding,
                    open_interest=self.as_float(item.get("openInterest")),
                    volume_24h=self.as_float(item.get("turnover24h")),
                )
            )
            if rate is not None and next_funding:
                current_funding.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=instrument.funding_interval_hours,
                        kind="current",
                    )
                )

        history: list[FundingRate] = []
        if include_history:
            batches = await asyncio.gather(
                *(self._history(item, history_since) for item in instruments),
                return_exceptions=True,
            )
            for batch in batches:
                if isinstance(batch, list):
                    history.extend(batch)
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current_funding + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/v5/market/funding/history",
            params={
                "category": "linear",
                "symbol": instrument.symbol,
                "startTime": int(since.timestamp() * 1000),
                "limit": 200,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return []
        observed = utc_now()
        return [
            FundingRate(
                venue=self.venue,
                symbol=instrument.symbol,
                underlying=instrument.underlying,
                observed_at=observed,
                effective_at=_from_millis(item.get("fundingRateTimestamp")) or observed,
                rate=float(item["fundingRate"]),
                interval_hours=instrument.funding_interval_hours,
                kind="settled",
            )
            for item in payload.get("result", {}).get("list", [])
        ]


class BitgetAdapter(VenueAdapter):
    venue = "bitget"
    base_url = "https://api.bitget.com"
    maker_fee = 0.0002
    taker_fee = 0.0006

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        info_response, ticker_response, funding_response = await asyncio.gather(
            self.client.get(
                f"{self.base_url}/api/v3/market/instruments",
                params={"category": "USDT-FUTURES"},
            ),
            self.client.get(
                f"{self.base_url}/api/v3/market/tickers",
                params={"category": "USDT-FUTURES"},
            ),
            self.client.get(
                f"{self.base_url}/api/v3/market/current-fund-rate",
                params={"category": "USDT-FUTURES"},
            ),
        )
        info_response.raise_for_status()
        ticker_response.raise_for_status()
        funding_response.raise_for_status()
        info_payload = info_response.json()
        ticker_payload = ticker_response.json()
        funding_payload = funding_response.json()
        if info_payload.get("code") != "00000":
            raise RuntimeError(info_payload.get("msg", "Bitget instruments failed"))

        instruments: list[Instrument] = []
        for item in info_payload.get("data", []):
            underlying = self.match_underlying(item.get("symbol", ""))
            if (
                not underlying
                or item.get("symbolType") != "stock"
                or item.get("type") != "perpetual"
                or item.get("status") != "online"
            ):
                continue
            interval = float(item.get("fundInterval") or 8)
            maker = self.as_float(item.get("makerFeeRate"))
            taker = self.as_float(item.get("takerFeeRate"))
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=item["symbol"],
                    underlying=underlying,
                    display_name=underlying,
                    quote_currency=item.get("quoteCoin", "USDT"),
                    funding_interval_hours=interval,
                    maker_fee=self.maker_fee if maker is None else maker,
                    taker_fee=self.taker_fee if taker is None else taker,
                    metadata={
                        "symbol_type": item.get("symbolType"),
                        "is_rwa": item.get("isRwa"),
                        "funding_rate_cap": item.get("fundingRateCap"),
                        "funding_rate_floor": item.get("fundingRateFloor"),
                    },
                )
            )

        instrument_map = {item.symbol: item for item in instruments}
        funding_map = {
            item.get("symbol"): item
            for item in (
                funding_payload.get("data", {}).get("resultList", [])
                if isinstance(funding_payload.get("data"), dict)
                else funding_payload.get("data", [])
            )
        }
        snapshots: list[MarketSnapshot] = []
        current_funding: list[FundingRate] = []
        for item in ticker_payload.get("data", []):
            instrument = instrument_map.get(item.get("symbol"))
            if not instrument:
                continue
            funding_item = funding_map.get(instrument.symbol, {})
            rate = self.as_float(funding_item.get("fundingRate") or item.get("fundingRate"))
            interval = self.as_float(funding_item.get("fundingRateInterval"))
            if interval:
                instrument.funding_interval_hours = interval
            next_funding = _from_millis(
                funding_item.get("nextUpdate") or item.get("nextFundingTime")
            )
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    bid=self.as_float(item.get("bid1Price")),
                    ask=self.as_float(item.get("ask1Price")),
                    mark_price=self.as_float(item.get("markPrice")),
                    index_price=self.as_float(item.get("indexPrice")),
                    funding_rate=rate,
                    funding_interval_hours=instrument.funding_interval_hours,
                    next_funding_at=next_funding,
                    open_interest=self.as_float(item.get("openInterest")),
                    volume_24h=self.as_float(item.get("turnover24h")),
                )
            )
            if rate is not None and next_funding:
                current_funding.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=instrument.funding_interval_hours,
                        kind="current",
                    )
                )

        history: list[FundingRate] = []
        if include_history:
            batches = await asyncio.gather(
                *(self._history(item, history_since) for item in instruments),
                return_exceptions=True,
            )
            for batch in batches:
                if isinstance(batch, list):
                    history.extend(batch)
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current_funding + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/api/v3/market/history-fund-rate",
            params={
                "category": "USDT-FUTURES",
                "symbol": instrument.symbol,
                "limit": 100,
                "cursor": 1,
            },
        )
        response.raise_for_status()
        payload = response.json()
        observed = utc_now()
        result: list[FundingRate] = []
        data = payload.get("data", {})
        rows = data.get("resultList", []) if isinstance(data, dict) else data
        for item in rows:
            effective = _from_millis(item.get("fundingRateTimestamp"))
            if not effective or effective < since:
                continue
            result.append(
                FundingRate(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=observed,
                    effective_at=effective,
                    rate=float(item["fundingRate"]),
                    interval_hours=instrument.funding_interval_hours,
                    kind="settled",
                )
            )
        return result


class GateAdapter(VenueAdapter):
    venue = "gate"
    base_url = "https://api.gateio.ws/api/v4"
    maker_fee = 0.0002
    taker_fee = 0.0005

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        contract_response, ticker_response = await asyncio.gather(
            self.client.get(f"{self.base_url}/futures/usdt/contracts"),
            self.client.get(f"{self.base_url}/futures/usdt/tickers"),
        )
        contract_response.raise_for_status()
        ticker_response.raise_for_status()
        instruments: list[Instrument] = []
        contracts: dict[str, dict[str, Any]] = {}
        for item in contract_response.json():
            underlying = self.match_underlying(item.get("name", ""))
            if (
                not underlying
                or item.get("in_delisting")
                or item.get("status") not in (None, "trading")
            ):
                continue
            contract_type = str(item.get("contract_type") or item.get("type") or "").lower()
            if contract_type and "stock" not in contract_type and item.get("name", "").replace("_", "").upper() != f"{underlying}USDT":
                continue
            interval = float(item.get("funding_interval") or 28800) / 3600
            maker = self.as_float(item.get("maker_fee_rate"))
            taker = self.as_float(item.get("taker_fee_rate"))
            instrument = Instrument(
                venue=self.venue,
                symbol=item["name"],
                underlying=underlying,
                display_name=underlying,
                quote_currency="USDT",
                funding_interval_hours=interval,
                maker_fee=self.maker_fee if maker is None else maker,
                taker_fee=self.taker_fee if taker is None else taker,
                metadata={
                    "contract_type": item.get("contract_type"),
                    "interest_rate": item.get("interest_rate"),
                    "funding_rate_limit": item.get("funding_rate_limit"),
                },
            )
            instruments.append(instrument)
            contracts[instrument.symbol] = item

        instrument_map = {item.symbol: item for item in instruments}
        snapshots: list[MarketSnapshot] = []
        current_funding: list[FundingRate] = []
        for item in ticker_response.json():
            instrument = instrument_map.get(item.get("contract"))
            if not instrument:
                continue
            contract = contracts.get(instrument.symbol, {})
            rate = self.as_float(item.get("funding_rate"))
            next_funding = _from_seconds(contract.get("funding_next_apply"))
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    bid=self.as_float(item.get("highest_bid")),
                    ask=self.as_float(item.get("lowest_ask")),
                    mark_price=self.as_float(item.get("mark_price")),
                    index_price=self.as_float(item.get("index_price")),
                    funding_rate=rate,
                    funding_interval_hours=instrument.funding_interval_hours,
                    next_funding_at=next_funding,
                    open_interest=self.as_float(item.get("total_size")),
                    volume_24h=self.as_float(item.get("volume_24h_quote") or item.get("volume_24h_usd")),
                )
            )
            if rate is not None and next_funding:
                current_funding.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        effective_at=next_funding,
                        rate=rate,
                        interval_hours=instrument.funding_interval_hours,
                        kind="current",
                    )
                )

        history: list[FundingRate] = []
        if include_history:
            batches = await asyncio.gather(
                *(self._history(item, history_since) for item in instruments),
                return_exceptions=True,
            )
            for batch in batches:
                if isinstance(batch, list):
                    history.extend(batch)
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=current_funding + history)

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/futures/usdt/funding_rate",
            params={
                "contract": instrument.symbol,
                "limit": 100,
                "from": int(since.timestamp()),
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
                effective_at=_from_seconds(item.get("t")) or observed,
                rate=float(item["r"]),
                interval_hours=instrument.funding_interval_hours,
                kind="settled",
            )
            for item in response.json()
        ]


class OkxAdapter(VenueAdapter):
    venue = "okx"
    base_url = "https://www.okx.com"
    maker_fee = 0.0002
    taker_fee = 0.0005

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        response = await self.client.get(
            f"{self.base_url}/api/v5/public/instruments",
            params={"instType": "SWAP"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0":
            raise RuntimeError(payload.get("msg", "OKX instruments failed"))
        instruments: list[Instrument] = []
        for item in payload.get("data", []):
            underlying = self.match_underlying(item.get("instId", ""))
            if not underlying or item.get("state") != "live":
                continue
            if str(item.get("instCategory") or "") != "3":
                continue
            if str(item.get("groupId") or "") not in ("6", ""):
                continue
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=item["instId"],
                    underlying=underlying,
                    display_name=underlying,
                    quote_currency=item.get("settleCcy", "USDT"),
                    funding_interval_hours=8,
                    maker_fee=self.maker_fee,
                    taker_fee=self.taker_fee,
                    metadata={
                        "inst_category": item.get("instCategory"),
                        "group_id": item.get("groupId"),
                        "uly": item.get("uly"),
                    },
                )
            )

        batches = await asyncio.gather(
            *(self._instrument_data(item, history_since, include_history) for item in instruments),
            return_exceptions=True,
        )
        snapshots: list[MarketSnapshot] = []
        funding: list[FundingRate] = []
        for batch in batches:
            if isinstance(batch, tuple):
                snapshot, current, history = batch
                if snapshot:
                    snapshots.append(snapshot)
                if current:
                    funding.append(current)
                funding.extend(history)
        return AdapterResult(instruments=instruments, snapshots=snapshots, funding=funding)

    async def _instrument_data(
        self,
        instrument: Instrument,
        history_since: datetime,
        include_history: bool,
    ) -> tuple[MarketSnapshot | None, FundingRate | None, list[FundingRate]]:
        calls = [
            self.client.get(f"{self.base_url}/api/v5/market/ticker", params={"instId": instrument.symbol}),
            self.client.get(f"{self.base_url}/api/v5/public/funding-rate", params={"instId": instrument.symbol}),
            self.client.get(
                f"{self.base_url}/api/v5/public/mark-price",
                params={"instType": "SWAP", "instId": instrument.symbol},
            ),
        ]
        if include_history:
            calls.append(
                self.client.get(
                    f"{self.base_url}/api/v5/public/funding-rate-history",
                    params={"instId": instrument.symbol, "limit": 100},
                )
            )
        responses = await asyncio.gather(*calls)
        for response in responses:
            response.raise_for_status()
        ticker = responses[0].json().get("data", [{}])[0]
        funding_data = responses[1].json().get("data", [{}])[0]
        mark_data = responses[2].json().get("data", [{}])[0]
        funding_time = _from_millis(funding_data.get("fundingTime"))
        next_time = _from_millis(funding_data.get("nextFundingTime"))
        interval = instrument.funding_interval_hours
        if funding_time and next_time:
            interval = max((next_time - funding_time).total_seconds() / 3600, 1)
            instrument.funding_interval_hours = interval
        rate = self.as_float(funding_data.get("fundingRate"))
        snapshot = MarketSnapshot(
            venue=self.venue,
            symbol=instrument.symbol,
            underlying=instrument.underlying,
            bid=self.as_float(ticker.get("bidPx")),
            ask=self.as_float(ticker.get("askPx")),
            mark_price=self.as_float(mark_data.get("markPx")),
            index_price=None,
            funding_rate=rate,
            funding_interval_hours=interval,
            next_funding_at=funding_time,
            open_interest=None,
            volume_24h=self.as_float(ticker.get("volCcy24h")),
        )
        current = None
        if rate is not None and funding_time:
            current = FundingRate(
                venue=self.venue,
                symbol=instrument.symbol,
                underlying=instrument.underlying,
                effective_at=funding_time,
                rate=rate,
                interval_hours=interval,
                kind="current",
            )
        history: list[FundingRate] = []
        if include_history and len(responses) > 3:
            observed = utc_now()
            for item in responses[3].json().get("data", []):
                effective = _from_millis(item.get("fundingTime"))
                if not effective or effective < history_since:
                    continue
                history.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        observed_at=observed,
                        effective_at=effective,
                        rate=float(item.get("realizedRate") or item.get("fundingRate")),
                        interval_hours=interval,
                        kind="settled",
                    )
                )
        return snapshot, current, history


class KrakenAdapter(VenueAdapter):
    venue = "kraken"
    base_url = "https://futures.kraken.com"
    maker_fee = 0.0002
    taker_fee = 0.0005

    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        instrument_response, ticker_response = await asyncio.gather(
            self.client.get(f"{self.base_url}/derivatives/api/v3/instruments"),
            self.client.get(f"{self.base_url}/derivatives/api/v3/tickers"),
        )
        instrument_response.raise_for_status()
        ticker_response.raise_for_status()
        instrument_payload = instrument_response.json()
        ticker_payload = ticker_response.json()
        instruments: list[Instrument] = []
        for item in instrument_payload.get("instruments", []):
            symbol = item.get("symbol", "")
            underlying = self.match_underlying(symbol)
            if (
                not underlying
                or not symbol.startswith("PF_")
                or item.get("tradeable") is False
                or item.get("tradfi") is not True
                or item.get("type") not in (None, "flexible_futures")
            ):
                continue
            instruments.append(
                Instrument(
                    venue=self.venue,
                    symbol=symbol,
                    underlying=underlying,
                    display_name=underlying,
                    quote_currency="USD",
                    funding_interval_hours=1,
                    maker_fee=self.maker_fee,
                    taker_fee=self.taker_fee,
                    metadata={
                        "type": item.get("type"),
                        "underlying": item.get("underlying"),
                        "funding_model": "continuous_accrual",
                        "funding_rate_coefficient": item.get("fundingRateCoefficient"),
                        "max_relative_funding_rate": item.get("maxRelativeFundingRate"),
                    },
                )
            )
        instrument_map = {item.symbol: item for item in instruments}
        snapshots: list[MarketSnapshot] = []
        current_funding: list[FundingRate] = []
        for item in ticker_payload.get("tickers", []):
            instrument = instrument_map.get(item.get("symbol"))
            if not instrument:
                continue
            index_price = self.as_float(item.get("indexPrice"))
            relative_rate = self.as_float(item.get("relativeFundingRate"))
            if relative_rate is None:
                absolute_rate = self.as_float(item.get("fundingRate"))
                relative_rate = absolute_rate / index_price if absolute_rate is not None and index_price else None
            observed = utc_now()
            next_hour = observed.replace(minute=0, second=0, microsecond=0)
            if next_hour <= observed:
                from datetime import timedelta

                next_hour += timedelta(hours=1)
            snapshots.append(
                MarketSnapshot(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    bid=self.as_float(item.get("bid")),
                    ask=self.as_float(item.get("ask")),
                    mark_price=self.as_float(item.get("markPrice")),
                    index_price=index_price,
                    funding_rate=relative_rate,
                    funding_interval_hours=1,
                    next_funding_at=next_hour,
                    open_interest=self.as_float(item.get("openInterest")),
                    volume_24h=self.as_float(item.get("vol24h")),
                )
            )
            if relative_rate is not None:
                current_funding.append(
                    FundingRate(
                        venue=self.venue,
                        symbol=instrument.symbol,
                        underlying=instrument.underlying,
                        effective_at=next_hour,
                        rate=relative_rate,
                        interval_hours=1,
                        kind="current",
                    )
                )
        history: list[FundingRate] = []
        if include_history:
            batches = await asyncio.gather(
                *(self._history(item, history_since) for item in instruments),
                return_exceptions=True,
            )
            for batch in batches:
                if isinstance(batch, list):
                    history.extend(batch)
        return AdapterResult(
            instruments=instruments,
            snapshots=snapshots,
            funding=current_funding + history,
        )

    async def _history(self, instrument: Instrument, since: datetime) -> list[FundingRate]:
        response = await self.client.get(
            f"{self.base_url}/derivatives/api/v3/historical-funding-rates",
            params={"symbol": instrument.symbol},
        )
        response.raise_for_status()
        payload = response.json()
        observed = utc_now()
        result: list[FundingRate] = []
        for item in payload.get("rates", []):
            raw_timestamp = item.get("timestamp")
            try:
                effective = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if effective < history_since:
                continue
            relative = self.as_float(item.get("relativeFundingRate"))
            if relative is None:
                continue
            result.append(
                FundingRate(
                    venue=self.venue,
                    symbol=instrument.symbol,
                    underlying=instrument.underlying,
                    observed_at=observed,
                    effective_at=effective,
                    rate=relative,
                    interval_hours=1,
                    kind="settled",
                )
            )
        return result
