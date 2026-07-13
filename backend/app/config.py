from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_prefix="CARRY_",
        extra="ignore",
    )

    database_path: Path = ROOT_DIR / "data" / "carry.duckdb"
    refresh_seconds: int = 30
    history_lookback_days: int = 7
    request_timeout_seconds: float = 12.0
    prediction_collection_enabled: bool = False
    prediction_hot_days: int = 90
    prediction_archive_dir: Path = ROOT_DIR / "data" / "prediction_archive"
    prediction_status_window_minutes: int = 60
    frontend_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    core_underlyings: str = "AAPL,NVDA,TSLA,GOOGL,SPY,QQQ,MSFT,AMZN,META,MSTR,HOOD,CRCL,COIN,PLTR"
    enabled_venues: str = "binance,bitget,bybit,gate,kraken,okx,lighter,extended,xyz,hotstuff,orderly"

    @property
    def origins(self) -> list[str]:
        return [value.strip() for value in self.frontend_origins.split(",") if value.strip()]

    @property
    def underlyings(self) -> set[str]:
        return {value.strip().upper() for value in self.core_underlyings.split(",") if value.strip()}

    @property
    def venues(self) -> set[str]:
        return {value.strip().lower() for value in self.enabled_venues.split(",") if value.strip()}


settings = Settings()
