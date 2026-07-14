from datetime import datetime, timezone

import httpx
import pytest

from backend.app.us_equity import collect_us_equity_quotes, get_us_spot_spec


def _yahoo_payload(price: float, timestamp: int) -> dict:
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": price,
                        "regularMarketTime": timestamp,
                        "currentTradingPeriod": {
                            "regular": {
                                "start": timestamp - 60,
                                "end": timestamp + 60,
                            }
                        },
                    },
                    "timestamp": [timestamp],
                    "indicators": {"quote": [{"close": [price]}]},
                }
            ],
        }
    }


def test_us_spot_specs_are_explicit_and_keep_skhynix_ads_separate() -> None:
    bb = get_us_spot_spec("BB")
    skhy = get_us_spot_spec("SKHY")

    assert bb is not None and bb.ticker == "BB" and bb.spot_units_per_perp_unit == 1
    assert skhy is not None
    assert skhy.ticker == "SKHY"
    assert skhy.quote_symbols == ("SKHY", "SKHYV")
    assert skhy.spot_units_per_perp_unit == 1
    assert get_us_spot_spec("SKHYNIX") is None
    assert get_us_spot_spec("SAMSUNG") is None


@pytest.mark.asyncio
async def test_quote_collection_uses_temporary_skhynix_symbol_as_delayed_fallback() -> None:
    timestamp = int(datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        if "query2.finance.yahoo.com" in request.url.host:
            ticker = request.url.path.rsplit("/", 1)[-1]
            if ticker == "BB":
                return httpx.Response(200, json=_yahoo_payload(10.97, timestamp))
            return httpx.Response(
                200,
                json={"chart": {"result": None, "error": {"description": "no data"}}},
            )
        ticker = request.url.path.split("/")[-2]
        if ticker == "SKHYV":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "primaryData": {
                            "lastSalePrice": "$168.01",
                            "lastTradeTimestamp": "Jul 10, 2026",
                            "isRealTime": False,
                        },
                        "marketStatus": "Closed",
                    }
                },
            )
        return httpx.Response(400, json={"data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collection = await collect_us_equity_quotes(client, {"BB", "SKHY"})

    assert collection.errors == ()
    snapshots = {item.symbol: item for item in collection.result.snapshots}
    assert snapshots["BB"].mark_price == pytest.approx(10.97)
    assert snapshots["SKHY"].mark_price == pytest.approx(168.01)
    assert snapshots["SKHY"].observed_at == datetime(
        2026, 7, 10, 20, 0, tzinfo=timezone.utc
    )
    instruments = {item.symbol: item for item in collection.result.instruments}
    assert instruments["SKHY"].metadata["provider_symbol"] == "SKHYV"
    assert instruments["SKHY"].metadata["quote_delayed"] is True
    assert instruments["SKHY"].metadata["spot_market"] == "US"
    assert instruments["SKHY"].metadata["local_currency"] == "USD"
