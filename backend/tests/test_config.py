from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from backend.app.config import Settings


def _cors_client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_origin_regex=settings.origin_regex,
        allow_methods=["GET"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


def test_local_cors_regex_accepts_arbitrary_dev_port() -> None:
    settings = Settings(_env_file=None)
    response = _cors_client(settings).get(
        "/health",
        headers={"Origin": "http://localhost:4318"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:4318"


def test_local_cors_regex_does_not_open_unconfigured_remote_origins() -> None:
    settings = Settings(_env_file=None)
    response = _cors_client(settings).get(
        "/health",
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_manual_refresh_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.manual_refresh_enabled is False


def test_history_backfill_defaults_to_one_venue_at_a_time() -> None:
    settings = Settings(_env_file=None)

    assert settings.history_venue_concurrency == 1


def test_production_cors_allows_only_the_exact_frontend_origin() -> None:
    settings = Settings(
        _env_file=None,
        frontend_origins="https://equity-carry-monitor.example.workers.dev",
        frontend_origin_regex="",
    )
    client = _cors_client(settings)

    accepted = client.get(
        "/health",
        headers={"Origin": "https://equity-carry-monitor.example.workers.dev"},
    )
    rejected = client.get(
        "/health",
        headers={"Origin": "https://attacker.example.workers.dev"},
    )
    localhost = client.get(
        "/health",
        headers={"Origin": "http://localhost:4318"},
    )

    assert accepted.headers["access-control-allow-origin"] == (
        "https://equity-carry-monitor.example.workers.dev"
    )
    assert "access-control-allow-origin" not in rejected.headers
    assert "access-control-allow-origin" not in localhost.headers
