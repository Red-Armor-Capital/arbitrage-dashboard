from datetime import datetime, timezone

import httpx
import pytest

from backend.app.kr_equity import collect_kr_equity_quotes, get_kr_spot_spec


def _naver_stock_payload() -> dict:
    return {
        "datas": [
            {
                "itemCode": "000660",
                "closePriceRaw": "1913000",
                "marketStatus": "CLOSE",
                "localTradedAt": "2026-07-14T15:30:00+09:00",
                "currencyType": {"code": "KRW"},
                "stockExchangeType": {
                    "zoneId": "Asia/Seoul",
                    "delayTime": 0,
                },
                "overMarketPriceInfo": {
                    "tradingSessionType": "AFTER_MARKET",
                    "overMarketStatus": "OPEN",
                    "overPrice": "1,919,000",
                    "localTradedAt": "2026-07-14T16:17:56+09:00",
                    "tradeStopType": {"name": "TRADING"},
                },
            }
        ]
    }


def _naver_fx_payload() -> dict:
    return {
        "isSuccess": True,
        "result": {
            "reutersCode": "FX_USDKRW",
            "calcPrice": "1495.8",
            "localTradedAt": "2026-07-14T16:13:23+09:00",
            "marketStatus": "OPEN",
            "stockExchangeType": {
                "name": "Hana Bank",
                "zoneId": "Asia/Seoul",
            },
        },
    }


def test_kr_spot_spec_maps_only_korean_common_share() -> None:
    spec = get_kr_spot_spec("SKHYNIX")

    assert spec is not None
    assert spec.ticker == "000660.KS"
    assert spec.spot_units_per_perp_unit == 1
    assert get_kr_spot_spec("SKHY") is None


@pytest.mark.asyncio
async def test_kr_quote_uses_extended_session_and_converts_krw_to_usd() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "polling.finance.naver.com" in request.url.host:
            return httpx.Response(200, json=_naver_stock_payload())
        if "m.stock.naver.com" in request.url.host:
            return httpx.Response(200, json=_naver_fx_payload())
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collection = await collect_kr_equity_quotes(client, {"SKHYNIX", "SKHY"})

    assert collection.errors == ()
    assert len(collection.result.snapshots) == 1
    snapshot = collection.result.snapshots[0]
    assert snapshot.venue == "kr_equity"
    assert snapshot.symbol == "000660.KS"
    assert snapshot.underlying == "SKHYNIX"
    assert snapshot.mark_price == pytest.approx(1_919_000 / 1495.8)
    assert snapshot.observed_at == datetime(
        2026, 7, 14, 7, 13, 23, tzinfo=timezone.utc
    )

    instrument = collection.result.instruments[0]
    assert instrument.metadata["quote_session"] == "post"
    assert instrument.metadata["local_price"] == 1_919_000
    assert instrument.metadata["local_currency"] == "KRW"
    assert instrument.metadata["local_per_usd"] == pytest.approx(1495.8)
    assert instrument.metadata["spot_market"] == "KR"


@pytest.mark.asyncio
async def test_kr_quote_fails_closed_when_both_fx_sources_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "polling.finance.naver.com" in request.url.host:
            return httpx.Response(200, json=_naver_stock_payload())
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collection = await collect_kr_equity_quotes(client, {"SKHYNIX"})

    assert collection.result.snapshots == []
    assert collection.errors
    assert "KRW=X" in collection.errors[0]
