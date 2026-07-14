from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.app.adapters.dex import (
    ExtendedAdapter,
    HotstuffAdapter,
    LighterAdapter,
    OrderlyAdapter,
    XyzAdapter,
    _seconds,
)
from backend.app.models import Instrument


HISTORY_SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
COLLECTION_TIME = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)


def test_seconds_parses_epoch_and_rejects_invalid_values() -> None:
    assert _seconds(1_783_764_000) == COLLECTION_TIME
    assert _seconds("not-a-timestamp") is None
    assert _seconds(0) is None


async def _collect(
    adapter_type: type,
    handler: Callable[[httpx.Request], httpx.Response],
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = adapter_type(client, set())
        return await adapter.collect(HISTORY_SINCE, include_history=False)


@pytest.mark.asyncio
async def test_lighter_discovers_reviewed_stock_and_uses_one_bulk_details_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/orderBooks":
            return httpx.Response(
                200,
                json={
                    "order_books": [
                        {
                            "symbol": "INTC",
                            "market_id": 7,
                            "status": "active",
                            "quote_symbol": "USDC",
                        },
                        {
                            "symbol": "BTC",
                            "market_id": 1,
                            "status": "active",
                            "quote_symbol": "USDC",
                        },
                    ]
                },
            )
        if request.url.path == "/api/v1/funding-rates":
            return httpx.Response(
                200,
                json={
                    "funding_rates": [
                        {"exchange": "lighter", "market_id": 7, "rate": "0.0008"}
                    ]
                },
            )
        if request.url.path == "/api/v1/orderBookDetails":
            return httpx.Response(
                200,
                json={
                    "order_book_details": [
                        {
                            "symbol": "INTC",
                            "market_id": 7,
                            "mark_price": "33.25",
                            "index_price": "33.20",
                            "current_funding_rate": "0.01",
                            "funding_timestamp": 1_784_000_000,
                            "open_interest": "1200",
                            "daily_quote_token_volume": "500000",
                        },
                        {"symbol": "BTC", "market_id": 1, "mark_price": "60000"},
                    ]
                },
            )
        if request.url.path == "/api/v1/orderBookOrders":
            return httpx.Response(200, json={"bids": [], "asks": []})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    result = await _collect(LighterAdapter, handler)

    assert [item.underlying for item in result.instruments] == ["INTC"]
    instrument = result.instruments[0]
    assert instrument.metadata["asset_class"] == "stock"
    assert instrument.metadata["spot_carry_eligible"] is True
    assert result.snapshots[0].mark_price == pytest.approx(33.25)
    snapshot = result.snapshots[0]
    assert snapshot.funding_rate == pytest.approx(0.0001)
    assert snapshot.next_funding_at == datetime(
        2026, 7, 11, 11, tzinfo=timezone.utc
    )
    assert result.funding[0].effective_at == snapshot.next_funding_at
    assert snapshot.target_source == "schedule"
    assert snapshot.raw_funding_rate == pytest.approx(0.01)
    assert snapshot.raw_rate_unit == "percent"
    assert snapshot.source_tenor_hours == 1
    assert snapshot.transform_version == "percent-to-decimal-v1"
    assert snapshot.open_interest == pytest.approx(1200)
    assert snapshot.volume_24h == pytest.approx(500000)

    details_requests = [
        request
        for request in requests
        if request.url.path == "/api/v1/orderBookDetails"
    ]
    assert len(details_requests) == 1
    assert not details_requests[0].url.query
    assert not any(
        request.url.path == "/api/v1/orderBookOrders" for request in requests
    )


@pytest.mark.asyncio
async def test_lighter_preserves_raw_normalized_eight_hour_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/orderBooks":
            return httpx.Response(
                200,
                json={
                    "order_books": [
                        {"symbol": "INTC", "market_id": 7, "status": "active"}
                    ]
                },
            )
        if request.url.path == "/api/v1/funding-rates":
            return httpx.Response(
                200,
                json={
                    "funding_rates": [
                        {"exchange": "lighter", "market_id": 7, "rate": "0.0008"}
                    ]
                },
            )
        if request.url.path == "/api/v1/orderBookDetails":
            return httpx.Response(
                200,
                json={
                    "order_book_details": [
                        {
                            "symbol": "INTC",
                            "market_id": 7,
                            "funding_rate": "9.9",
                            "funding_timestamp": 1_784_000_000,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    result = await _collect(LighterAdapter, handler)

    snapshot = result.snapshots[0]
    assert snapshot.funding_rate == pytest.approx(0.0001)
    assert snapshot.raw_funding_rate == pytest.approx(0.0008)
    assert snapshot.raw_rate_unit == "decimal"
    assert snapshot.source_tenor_hours == 8
    assert snapshot.transform_version == "eight-hour-to-hourly-v1"
    assert snapshot.next_funding_at == datetime(
        2026, 7, 11, 11, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_extended_discovers_all_active_visible_tradfi_equities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)
    api_target = COLLECTION_TIME + timedelta(minutes=30)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/info/markets"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "INTC-USD",
                        "uiName": "INTC",
                        "category": "TradFi",
                        "subCategory": "Equity",
                        "visibleOnUi": True,
                        "status": "ACTIVE",
                        "marketStats": {
                            "markPrice": "34.1",
                            "indexPrice": "34.0",
                            "fundingRate": "0.0002",
                            "nextFundingTime": int(api_target.timestamp() * 1000),
                        },
                    },
                    {
                        "name": "HIDDEN-USD",
                        "uiName": "HIDDEN",
                        "category": "TradFi",
                        "subCategory": "Equity",
                        "visibleOnUi": False,
                        "status": "ACTIVE",
                    },
                    {
                        "name": "BTC-USD",
                        "uiName": "BTC",
                        "category": "Crypto",
                        "subCategory": "Crypto",
                        "visibleOnUi": True,
                        "status": "ACTIVE",
                    },
                ]
            },
        )

    result = await _collect(ExtendedAdapter, handler)

    assert [item.underlying for item in result.instruments] == ["INTC"]
    assert result.instruments[0].metadata["asset_class"] == "stock"
    assert result.instruments[0].metadata["spot_carry_eligible"] is True
    snapshot = result.snapshots[0]
    assert snapshot.funding_rate == pytest.approx(0.0002)
    assert snapshot.next_funding_at == api_target
    assert snapshot.target_source == "api"
    assert snapshot.raw_funding_rate == pytest.approx(0.0002)
    assert snapshot.raw_rate_unit == "decimal"
    assert snapshot.source_tenor_hours == 1
    assert snapshot.transform_version == "identity-v1"


@pytest.mark.asyncio
async def test_extended_rejects_non_future_api_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/info/markets"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "INTC-USD",
                        "uiName": "INTC",
                        "category": "TradFi",
                        "subCategory": "Equity",
                        "visibleOnUi": True,
                        "status": "ACTIVE",
                        "marketStats": {
                            "fundingRate": "0",
                            "nextFundingRate": int(COLLECTION_TIME.timestamp() * 1000),
                        },
                    }
                ]
            },
        )

    result = await _collect(ExtendedAdapter, handler)

    snapshot = result.snapshots[0]
    assert snapshot.funding_rate == 0
    assert snapshot.next_funding_at == datetime(
        2026, 7, 11, 11, tzinfo=timezone.utc
    )
    assert snapshot.target_source == "schedule"
    assert result.funding[0].effective_at == snapshot.next_funding_at


@pytest.mark.asyncio
async def test_extended_parses_compact_settled_funding_fields() -> None:
    settled_at_ms = 1_783_900_800_000

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/info/INTC-USD/funding"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "m": "INTC-USD",
                        "f": "0.000125",
                        "T": settled_at_ms,
                    }
                ]
            },
        )

    instrument = Instrument(
        venue="extended",
        symbol="INTC-USD",
        underlying="INTC",
        funding_interval_hours=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        history = await ExtendedAdapter(client, set())._history(
            instrument,
            HISTORY_SINCE,
        )

    assert len(history) == 1
    assert history[0].symbol == "INTC-USD"
    assert history[0].underlying == "INTC"
    assert history[0].rate == pytest.approx(0.000125)
    assert history[0].effective_at == datetime.fromtimestamp(
        settled_at_ms / 1000,
        tz=timezone.utc,
    )
    assert history[0].kind == "settled"


def _xyz_handler(categories: list[list[str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "metaAndAssetCtxs":
            assert body["dex"] == "xyz"
            return httpx.Response(
                200,
                json=[
                    {
                        "universe": [
                            {
                                "name": "SKHX",
                                "growthMode": "enabled",
                                "maxLeverage": 5,
                            },
                            {"name": "BTC", "maxLeverage": 20},
                            {"name": "AAPL", "isDelisted": True},
                        ]
                    },
                    [
                        {
                            "impactPxs": ["100", "100.2"],
                            "markPx": "100.1",
                            "oraclePx": "100",
                            "funding": "0.0003",
                            "openInterest": "1200",
                            "dayNtlVlm": "500000",
                        },
                        {"markPx": "60000", "funding": "0.00001"},
                        {"markPx": "200", "funding": "0.00001"},
                    ],
                ],
            )
        if body["type"] == "perpCategories":
            return httpx.Response(200, json=categories)
        raise AssertionError(f"Unexpected request body: {body}")

    return handler


@pytest.mark.asyncio
async def test_xyz_uses_stock_category_and_canonicalizes_skhx_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)
    result = await _collect(
        XyzAdapter,
        _xyz_handler([["xyz:SKHX", "stocks"], ["xyz:BTC", "crypto"]]),
    )

    assert [item.symbol for item in result.instruments] == ["xyz:SKHX"]
    instrument = result.instruments[0]
    assert instrument.underlying == "SKHYNIX"
    assert instrument.metadata["asset_class"] == "stock"
    assert instrument.metadata["spot_carry_eligible"] is True
    assert instrument.metadata["growth_mode"] is True
    assert instrument.maker_fee == pytest.approx(0.00003)
    assert instrument.taker_fee == pytest.approx(0.00009)
    snapshot = result.snapshots[0]
    assert snapshot.next_funding_at == datetime(
        2026, 7, 11, 11, tzinfo=timezone.utc
    )
    assert snapshot.target_source == "schedule"
    assert snapshot.raw_funding_rate == pytest.approx(0.0003)
    assert snapshot.source_tenor_hours == 1
    assert snapshot.open_interest == pytest.approx(1200)
    assert snapshot.volume_24h == pytest.approx(500000)
    assert result.funding[0].effective_at == snapshot.next_funding_at


@pytest.mark.asyncio
async def test_xyz_fails_closed_when_stock_category_is_unavailable() -> None:
    try:
        result = await _collect(XyzAdapter, _xyz_handler([]))
    except RuntimeError:
        return

    assert result.instruments == []
    assert result.snapshots == []


@pytest.mark.asyncio
async def test_hotstuff_uses_price_index_and_excludes_delisted_instruments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "instruments":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 1,
                            "symbol": "X-PERP",
                            "price_index": "SPACEX/USD",
                            "status": "active",
                        },
                        {
                            "id": 2,
                            "symbol": "USA100-PERP",
                            "price_index": "USA100/USD",
                            "status": "active",
                        },
                        {
                            "id": 3,
                            "symbol": "AAPL-PERP",
                            "price_index": "AAPL/USD",
                            "status": "delisted",
                        },
                        {
                            "id": 4,
                            "symbol": "BTC-PERP",
                            "price_index": "BTC/USD",
                            "status": "active",
                        },
                    ]
                },
            )
        if body["method"] == "ticker":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"symbol": "X-PERP", "mark_price": "250", "funding_rate": "0.0004"},
                        {"symbol": "USA100-PERP", "mark_price": "23000", "funding_rate": "0.0001"},
                        {"symbol": "AAPL-PERP", "mark_price": "210", "funding_rate": "0.0002"},
                    ]
                },
            )
        raise AssertionError(f"Unexpected request body: {body}")

    result = await _collect(HotstuffAdapter, handler)

    assert [(item.symbol, item.underlying) for item in result.instruments] == [
        ("X-PERP", "SPACEX"),
        ("USA100-PERP", "US100"),
    ]
    assert result.instruments[0].metadata["asset_class"] == "preipo"
    assert result.instruments[0].metadata["spot_carry_eligible"] is False
    assert result.instruments[1].metadata["asset_class"] == "index"
    assert result.instruments[1].metadata["spot_carry_eligible"] is False
    assert result.instruments[0].metadata["current_rate_tenor_hours"] == 1
    assert (
        result.instruments[0].metadata["ticker_rate_semantics"]
        == "hourly_payment_rate"
    )
    snapshot = result.snapshots[0]
    assert snapshot.funding_rate == pytest.approx(0.0004)
    assert snapshot.raw_funding_rate == pytest.approx(0.0004)
    assert snapshot.raw_rate_unit == "decimal"
    assert snapshot.source_tenor_hours == 1
    assert snapshot.transform_version == "identity-v1"
    assert snapshot.next_funding_at == datetime(
        2026, 7, 11, 11, tzinfo=timezone.utc
    )
    assert result.funding[0].rate == pytest.approx(0.0004)
    assert result.funding[0].interval_hours == 1


@pytest.mark.asyncio
async def test_hotstuff_uses_exact_public_payment_history_and_deduplicates() -> None:
    settled_at = "2026-07-10T12:00:00.500Z"
    rows = [
        {
            "instrument_id": 18,
            "funding_rate": "0.001",
            "funding_payment": "0.2",
            "size": "2",
            "mark_price": "100",
            "side": side,
            "timestamp": settled_at,
        }
        for side in ("LONG", "SHORT")
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "funding_history"
        assert body["params"]["page"] == 1
        assert body["params"]["limit"] == 500
        return httpx.Response(
            200,
            json={
                "data": rows,
                "page": 1,
                "limit": 500,
                "total_count": 2,
                "total_pages": 1,
                "has_next": False,
            },
        )

    instrument = Instrument(
        venue="hotstuff",
        symbol="AAPL-PERP",
        underlying="AAPL",
        funding_interval_hours=1,
        metadata={"instrument_id": 18, "spot_carry_eligible": True},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await HotstuffAdapter(client, set()).collect_history(
            [instrument],
            HISTORY_SINCE,
        )

    assert len(batch.outcomes) == 1
    assert batch.outcomes[0].success is True
    assert len(batch.funding) == 1
    assert batch.funding[0].effective_at == datetime(
        2026, 7, 10, 12, tzinfo=timezone.utc
    )
    assert batch.funding[0].rate == pytest.approx(0.001)
    assert batch.funding[0].interval_hours == 1
    assert batch.funding[0].kind == "settled"


@pytest.mark.asyncio
async def test_hotstuff_discards_conflicting_settled_rates() -> None:
    rows = [
        {
            "instrument_id": 18,
            "funding_rate": str(rate),
            "funding_payment": str(2 * 100 * rate),
            "size": "2",
            "mark_price": "100",
            "timestamp": "2026-07-10T12:00:00.500Z",
        }
        for rate in (0.001, 0.002)
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": rows,
                "total_count": 2,
                "total_pages": 1,
                "has_next": False,
            },
        )

    instrument = Instrument(
        venue="hotstuff",
        symbol="AAPL-PERP",
        underlying="AAPL",
        metadata={"instrument_id": 18, "spot_carry_eligible": True},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await HotstuffAdapter(client, set()).collect_history(
            [instrument],
            datetime.now(timezone.utc) - timedelta(days=7),
        )

    assert batch.funding == []
    assert batch.outcomes[0].success is False
    assert "conflicting" in (batch.outcomes[0].error or "")


@pytest.mark.asyncio
async def test_orderly_includes_native_and_reviewed_mythos_stocks_not_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)
    api_target = COLLECTION_TIME + timedelta(minutes=30)
    info_rows = [
        {
            "symbol": "PERP_NVDA_USDC",
            "display_symbol_name": "NVDA",
            "status": "ACTIVE",
            "funding_period": 3_600_000,
        },
        {
            "symbol": "PERP_SKHYNIX_USDC",
            "display_symbol_name": "SKHYNIX",
            "broker_id": "mythos",
            "status": "ACTIVE",
            "funding_period": 3_600_000,
        },
        {
            "symbol": "PERP_BTC_USDC",
            "display_symbol_name": "BTC",
            "broker_id": "mythos",
            "status": "ACTIVE",
            "funding_period": 3_600_000,
        },
        {
            "symbol": "PERP_INTC_USDC",
            "display_symbol_name": "INTC",
            "broker_id": "other",
            "status": "ACTIVE",
            "funding_period": 3_600_000,
        },
        {
            "symbol": "PERP_AAPL_USDC",
            "display_symbol_name": "AAPL",
            "broker_id": "mythos",
            "status": "SUSPENDED",
            "funding_period": 3_600_000,
        },
    ]
    included_symbols = ["PERP_NVDA_USDC", "PERP_SKHYNIX_USDC"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/public/info":
            return httpx.Response(200, json={"data": {"rows": info_rows}})
        if request.url.path == "/v1/public/futures":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rows": [
                            {
                                "symbol": symbol,
                                "mark_price": "100",
                                "index_price": "100",
                                "open_interest": "1200",
                                "24h_volume": "5000",
                                "24h_amount": "500000",
                            }
                            for symbol in included_symbols
                        ]
                    }
                },
            )
        if request.url.path == "/v1/public/funding_rates":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rows": [
                            {
                                "symbol": symbol,
                                "est_funding_rate": "0.0002",
                                "next_funding_time": int(api_target.timestamp() * 1000),
                            }
                            for symbol in included_symbols
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    result = await _collect(OrderlyAdapter, handler)

    assert [(item.symbol, item.underlying) for item in result.instruments] == [
        ("PERP_NVDA_USDC", "NVDA"),
        ("PERP_SKHYNIX_USDC", "SKHYNIX"),
    ]
    assert all(
        item.metadata["asset_class"] == "stock" for item in result.instruments
    )
    assert all(
        item.metadata["spot_carry_eligible"] is True for item in result.instruments
    )
    assert len(result.snapshots) == 2
    assert len(result.funding) == 2
    assert all(item.next_funding_at == api_target for item in result.snapshots)
    assert all(item.target_source == "api" for item in result.snapshots)
    assert all(item.raw_funding_rate == pytest.approx(0.0002) for item in result.snapshots)
    assert all(item.source_tenor_hours == 1 for item in result.snapshots)
    assert all(item.open_interest == pytest.approx(1200) for item in result.snapshots)
    assert all(item.volume_24h == pytest.approx(500000) for item in result.snapshots)


@pytest.mark.asyncio
async def test_orderly_derives_strict_standard_utc_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: COLLECTION_TIME)
    definitions = [
        ("PERP_NVDA_USDC", "NVDA", None, 1),
        ("PERP_GOOGL_USDC", "GOOGL", None, 2),
        ("PERP_TSLA_USDC", "TSLA", None, 4),
        ("PERP_AAPL_USDC", "AAPL", "mythos", 8),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/public/info":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rows": [
                            {
                                "symbol": symbol,
                                "display_symbol_name": display,
                                "broker_id": broker,
                                "status": "ACTIVE",
                                "funding_period": interval * 3_600_000,
                            }
                            for symbol, display, broker, interval in definitions
                        ]
                    }
                },
            )
        if request.url.path == "/v1/public/futures":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rows": [
                            {"symbol": symbol, "mark_price": "100"}
                            for symbol, _, _, _ in definitions
                        ]
                    }
                },
            )
        if request.url.path == "/v1/public/funding_rates":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rows": [
                            {
                                "symbol": symbol,
                                "est_funding_rate": "0",
                                "next_funding_time": int(
                                    COLLECTION_TIME.timestamp() * 1000
                                ),
                            }
                            for symbol, _, _, _ in definitions
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    result = await _collect(OrderlyAdapter, handler)

    expected = {
        "PERP_NVDA_USDC": datetime(2026, 7, 11, 11, tzinfo=timezone.utc),
        "PERP_GOOGL_USDC": datetime(2026, 7, 11, 12, tzinfo=timezone.utc),
        "PERP_TSLA_USDC": datetime(2026, 7, 11, 12, tzinfo=timezone.utc),
        "PERP_AAPL_USDC": datetime(2026, 7, 11, 16, tzinfo=timezone.utc),
    }
    assert {item.symbol: item.next_funding_at for item in result.snapshots} == expected
    assert all(item.target_source == "schedule" for item in result.snapshots)
    assert all(item.raw_funding_rate == 0 for item in result.snapshots)
    assert {item.symbol: item.effective_at for item in result.funding} == expected


@pytest.mark.asyncio
async def test_orderly_history_infers_supported_transitions_and_skips_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    monkeypatch.setattr("backend.app.adapters.dex.utc_now", lambda: history_now)
    since = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)

    def ms(value: datetime) -> int:
        return int(value.timestamp() * 1000)

    rows = [
        {
            "funding_rate_timestamp": ms(
                datetime(2026, 7, 11, 7, tzinfo=timezone.utc)
            ),
            "funding_rate": "0.005",
        },
        {
            "funding_rate_timestamp": ms(
                datetime(2026, 7, 10, 22, tzinfo=timezone.utc)
            ),
            "funding_rate": "0.004",
        },
        {
            "funding_rate_timestamp": ms(
                datetime(2026, 7, 10, 7, 0, 30, tzinfo=timezone.utc)
            ),
            "funding_rate": "0.000",
        },
        {
            "funding_rate_timestamp": ms(
                datetime(2026, 7, 10, 14, tzinfo=timezone.utc)
            ),
            "funding_rate": "0.003",
        },
        {
            "funding_rate_timestamp": ms(
                datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
            ),
            "funding_rate": "0.001",
        },
        {
            "funding_rate_timestamp": ms(
                datetime(2026, 7, 10, 10, tzinfo=timezone.utc)
            ),
            "funding_rate": "0.002",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/public/funding_rate_history"
        assert int(request.url.params["start_t"]) == ms(
            since - timedelta(hours=8)
        )
        assert request.url.params["size"] == "500"
        return httpx.Response(
            200,
            json={"success": True, "data": {"rows": rows}},
        )

    instrument = Instrument(
        venue="orderly",
        symbol="PERP_NVDA_USDC",
        underlying="NVDA",
        funding_interval_hours=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        history = await OrderlyAdapter(client, set())._history(instrument, since)

    assert [item.effective_at for item in history] == [
        datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
        datetime(2026, 7, 10, 10, tzinfo=timezone.utc),
        datetime(2026, 7, 10, 14, tzinfo=timezone.utc),
        datetime(2026, 7, 10, 22, tzinfo=timezone.utc),
    ]
    assert [item.interval_hours for item in history] == [1, 2, 4, 8]
    assert [item.rate for item in history] == pytest.approx(
        [0.001, 0.002, 0.003, 0.004]
    )
