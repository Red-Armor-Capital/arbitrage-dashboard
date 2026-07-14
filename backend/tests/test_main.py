from fastapi import HTTPException
import pytest

from backend.app import main


@pytest.mark.asyncio
async def test_manual_refresh_disabled_does_not_touch_upstreams(monkeypatch) -> None:
    calls = 0

    async def fake_refresh_once() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(main.settings, "manual_refresh_enabled", False)
    monkeypatch.setattr(main.service, "refresh_once", fake_refresh_once)

    with pytest.raises(HTTPException) as exc_info:
        await main.refresh()

    assert exc_info.value.status_code == 404
    assert calls == 0


@pytest.mark.asyncio
async def test_manual_refresh_can_be_explicitly_enabled(monkeypatch) -> None:
    calls = 0

    async def fake_refresh_once() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(main.settings, "manual_refresh_enabled", True)
    monkeypatch.setattr(main.service, "refresh_once", fake_refresh_once)

    assert await main.refresh() == {"status": "ok"}
    assert calls == 1
