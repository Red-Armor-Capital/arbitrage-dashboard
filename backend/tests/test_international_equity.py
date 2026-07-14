from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.app import international_equity
from backend.app.international_equity import collect_international_equity_quotes
from backend.app.us_equity import EquityQuote
from backend.app.security_registry import get_security


def _spec(
    *,
    security_id: str = "HK:0700",
    underlying: str = "TENCENT",
    market: str = "HK",
    mic: str = "XHKG",
    ticker: str = "0700.HK",
    local_currency: str = "HKD",
    timezone_name: str = "Asia/Hong_Kong",
    spot_venue: str = "hk_equity",
    fx_symbol: str | None = "HKD=X",
) -> SimpleNamespace:
    return SimpleNamespace(
        security_id=security_id,
        underlying=underlying,
        display_name=underlying,
        asset_class="stock",
        market=market,
        mic=mic,
        ticker=ticker,
        quote_symbols=(ticker,),
        local_currency=local_currency,
        timezone=timezone_name,
        spot_venue=spot_venue,
        fx_symbol=fx_symbol,
    )


def _quote(
    symbol: str,
    price: float,
    observed_at: datetime,
    *,
    session: str = "regular",
    delayed: bool = False,
) -> EquityQuote:
    return EquityQuote(
        ticker=symbol,
        provider_symbol=symbol,
        price=price,
        observed_at=observed_at,
        source="Yahoo chart",
        session=session,
        is_delayed=delayed,
    )


@pytest.mark.asyncio
async def test_converts_local_price_and_uses_older_stock_or_fx_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stock_at = datetime(2026, 7, 14, 4, 15, tzinfo=timezone.utc)
    fx_at = datetime(2026, 7, 14, 4, 10, tzinfo=timezone.utc)

    async def fake_fetch(_client: httpx.AsyncClient, symbol: str) -> EquityQuote:
        if symbol == "HKD=X":
            return _quote(symbol, 7.84, fx_at)
        return _quote(symbol, 78.4, stock_at, delayed=True)

    monkeypatch.setattr(international_equity, "fetch_yahoo_quote", fake_fetch)
    async with httpx.AsyncClient() as client:
        collection = await collect_international_equity_quotes(client, [_spec()])

    assert collection.errors == ()
    snapshot = collection.result.snapshots[0]
    assert snapshot.venue == "hk_equity"
    assert snapshot.mark_price == pytest.approx(10.0)
    assert snapshot.observed_at == fx_at

    metadata = collection.result.instruments[0].metadata
    required = {
        "security_id",
        "mic",
        "market",
        "ticker",
        "local_currency",
        "local_price",
        "fx_symbol",
        "fx_rate",
        "quote_valid",
        "delay_status",
        "source",
        "observed_at",
    }
    assert required <= metadata.keys()
    assert metadata["local_price"] == pytest.approx(78.4)
    assert metadata["fx_rate"] == pytest.approx(7.84)
    assert metadata["quote_valid"] is True
    assert metadata["delay_status"] == "delayed"
    assert metadata["observed_at"] == fx_at.isoformat()


@pytest.mark.asyncio
async def test_fx_failure_marks_instrument_invalid_and_emits_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)

    async def fake_fetch(_client: httpx.AsyncClient, symbol: str) -> EquityQuote:
        if symbol == "JPY=X":
            raise httpx.ConnectError("FX unavailable")
        return _quote(symbol, 10_000, observed_at)

    monkeypatch.setattr(international_equity, "fetch_yahoo_quote", fake_fetch)
    spec = _spec(
        security_id="JP:9984",
        underlying="SOFTBANK",
        market="JP",
        mic="XTKS",
        ticker="9984.T",
        local_currency="JPY",
        timezone_name="Asia/Tokyo",
        spot_venue="jp_equity",
        fx_symbol="JPY=X",
    )
    async with httpx.AsyncClient() as client:
        collection = await collect_international_equity_quotes(client, [spec])

    assert collection.result.snapshots == []
    assert collection.errors and "JPY=X" in collection.errors[0]
    metadata = collection.result.instruments[0].metadata
    assert metadata["quote_valid"] is False
    assert metadata["delay_status"] == "unavailable"
    assert metadata["local_price"] == 10_000
    assert metadata["fx_rate"] is None
    assert metadata["observed_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "stock_at"),
    [
        (
            _spec(),
            datetime(2026, 7, 14, 4, 30, tzinfo=timezone.utc),  # 12:30 HKT
        ),
        (
            _spec(
                security_id="JP:285A",
                underlying="KIOXIA",
                market="JP",
                mic="XTKS",
                ticker="285A.T",
                local_currency="JPY",
                timezone_name="Asia/Tokyo",
                spot_venue="jp_equity",
                fx_symbol="JPY=X",
            ),
            datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc),  # 12:00 JST
        ),
    ],
)
async def test_hk_and_jp_lunch_break_overrides_yahoo_regular_session(
    monkeypatch: pytest.MonkeyPatch,
    spec: SimpleNamespace,
    stock_at: datetime,
) -> None:
    async def fake_fetch(_client: httpx.AsyncClient, symbol: str) -> EquityQuote:
        if symbol.endswith("=X"):
            return _quote(symbol, 100.0, stock_at)
        return _quote(symbol, 1_000.0, stock_at, session="regular")

    monkeypatch.setattr(international_equity, "fetch_yahoo_quote", fake_fetch)
    async with httpx.AsyncClient() as client:
        collection = await collect_international_equity_quotes(client, [spec])

    assert collection.result.instruments[0].metadata["quote_session"] == "break"
    assert len(collection.result.snapshots) == 1


@pytest.mark.asyncio
async def test_accepts_the_shipped_security_registry_dataclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)

    async def fake_fetch(_client: httpx.AsyncClient, symbol: str) -> EquityQuote:
        return _quote(
            symbol,
            7.84 if symbol == "HKD=X" else 235.2,
            observed_at,
            delayed=True,
        )

    monkeypatch.setattr(international_equity, "fetch_yahoo_quote", fake_fetch)
    spec = get_security("HK:XHKG:0100")
    assert spec is not None
    async with httpx.AsyncClient() as client:
        collection = await collect_international_equity_quotes(client, [spec])

    assert collection.result.snapshots[0].mark_price == pytest.approx(30.0)
    assert collection.result.instruments[0].metadata["security_id"] == spec.security_id
