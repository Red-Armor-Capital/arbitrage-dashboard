from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .registry import adapter_factories
from .service import CarryService
from .storage import CarryStore


store = CarryStore(settings.database_path)
service = CarryService(settings, store, adapter_factories())


@asynccontextmanager
async def lifespan(_: FastAPI):
    await service.start()
    yield
    await service.stop()


app = FastAPI(
    title="Equity Carry Monitor API",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_origin_regex=settings.origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dashboard")
async def dashboard():
    return service.dashboard()


@app.get("/api/venues")
async def venues():
    return store.get_statuses()


@app.get("/api/prediction-collector/status")
async def prediction_collector_status():
    return service.prediction_collector_status()


@app.post("/api/refresh")
async def refresh():
    await service.refresh_once()
    return {"status": "ok"}
