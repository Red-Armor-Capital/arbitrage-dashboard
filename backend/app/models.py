from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Instrument(BaseModel):
    venue: str
    symbol: str
    underlying: str
    display_name: str | None = None
    product_type: Literal["perpetual", "spot", "stock"] = "perpetual"
    quote_currency: str = "USD"
    funding_interval_hours: float = 8.0
    maker_fee: float = 0.0
    taker_fee: float = 0.0
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class MarketSnapshot(BaseModel):
    venue: str
    symbol: str
    underlying: str
    observed_at: datetime = Field(default_factory=utc_now)
    bid: float | None = None
    ask: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    funding_interval_hours: float = 8.0
    next_funding_at: datetime | None = None
    source_observed_at: datetime | None = None
    target_source: Literal["api", "schedule"] = "schedule"
    raw_funding_rate: float | None = None
    raw_rate_unit: Literal["decimal", "percent"] = "decimal"
    source_tenor_hours: float | None = None
    transform_version: str = "v1"
    open_interest: float | None = None
    volume_24h: float | None = None


class FundingRate(BaseModel):
    venue: str
    symbol: str
    underlying: str
    observed_at: datetime = Field(default_factory=utc_now)
    effective_at: datetime
    rate: float
    interval_hours: float
    kind: Literal["current", "settled"]

    @property
    def hourly_rate(self) -> float:
        return self.rate / self.interval_hours if self.interval_hours else 0.0


class HistoryFetchOutcome(BaseModel):
    """Result of fetching one instrument's settled funding history.

    ``success`` is deliberately independent from ``funding`` so that an empty,
    valid response can be scheduled like any other successful refresh while a
    request or parsing failure remains eligible for an earlier retry.
    """

    instrument: Instrument
    success: bool
    funding: list[FundingRate] = Field(default_factory=list)
    error: str | None = None


class HistoryBatchResult(BaseModel):
    outcomes: list[HistoryFetchOutcome] = Field(default_factory=list)

    @property
    def funding(self) -> list[FundingRate]:
        return [rate for outcome in self.outcomes for rate in outcome.funding]

    @property
    def failures(self) -> list[HistoryFetchOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.success]


class VenueStatus(BaseModel):
    venue: str
    status: Literal["healthy", "degraded", "offline", "not_configured"]
    last_success_at: datetime | None = None
    last_error: str | None = None
    instruments: int = 0
    latency_ms: float | None = None


class PredictionCollectorVenueStatus(BaseModel):
    venue: str
    status: Literal["warming_up", "healthy", "degraded", "offline"]
    last_success_at: datetime | None = None
    last_flushed_minute: datetime | None = None
    expected_symbols: int = 0
    sampled_symbols_last_minute: int = 0
    coverage_60m: float = 0.0
    missed_minutes_60m: int = 0
    hot_rows: int = 0
    archived_rows: int = 0
    last_error: str | None = None
    updated_at: datetime


class PredictionCollectorStatusResponse(BaseModel):
    generated_at: datetime = Field(default_factory=utc_now)
    enabled: bool
    poll_seconds: int
    sample_resolution_seconds: int = 60
    status_window_minutes: int
    hot_retention_days: int
    archive_dir: str
    last_archive_checked_at: datetime | None = None
    last_archive_error: str | None = None
    hot_rows: int
    archived_rows: int
    latest_archived_month: date | None = None
    venues: list[PredictionCollectorVenueStatus]


class PerpLiquiditySnapshot(BaseModel):
    venue: str
    symbol: str
    volume_24h_usd: float | None = None
    open_interest_usd: float | None = None


class CarryOpportunity(BaseModel):
    underlying: str
    display_name: str | None = None
    asset_class: Literal["stock", "etf", "index", "preipo", "basket", "unknown"]
    strategy_type: Literal["perp_perp", "spot_perp"]
    price_assumption: Literal[
        "observed",
        "spot_equals_perp",
        "spot_quote",
        "us_spot_quote",
        "kr_spot_quote",
    ]
    fee_scope: Literal["both_legs", "perp_leg_only"]
    long_venue: str
    long_symbol: str
    short_venue: str
    short_symbol: str
    current_carry_apr: float
    current_rate_kind: Literal["indicative"] = "indicative"
    mean_carry_apr: float | None
    carry_apr_volatility: float | None
    positive_ratio: float | None
    round_trip_fee_pct: float
    breakeven_hours: float | None
    indicative_breakeven_hours: float | None
    sample_hours: int
    history_quality: Literal["sufficient", "limited", "unavailable"]
    long_funding_apr: float
    short_funding_apr: float
    long_liquidity: PerpLiquiditySnapshot | None = None
    short_liquidity: PerpLiquiditySnapshot
    cross_basis_pct: float | None = None
    spot_market: Literal["US", "KR", "HK", "JP", "TW"] | None = None
    spot_security_id: str | None = None
    spot_mic: str | None = None
    spot_symbol: str | None = None
    spot_price_local: float | None = None
    spot_currency: str | None = None
    spot_local_per_usd: float | None = None
    spot_fx_symbol: str | None = None
    spot_price_usd: float | None = None
    spot_equivalent_price_usd: float | None = None
    spot_units_per_perp_unit: float | None = None
    perp_price_usd: float | None = None
    perp_price_kind: Literal["mark", "index"] | None = None
    spot_perp_basis_pct: float | None = None
    spot_quote_source: str | None = None
    spot_quote_session: str | None = None
    spot_quote_delayed: bool | None = None
    spot_observed_at: datetime | None = None
    perp_observed_at: datetime | None = None
    price_comparison_note: str | None = None
    data_freshness_seconds: float | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class DashboardSummary(BaseModel):
    opportunities: int
    best_carry_apr: float | None
    median_breakeven_hours: float | None
    stable_opportunities: int
    venues_healthy: int
    venues_total: int


class DashboardResponse(BaseModel):
    generated_at: datetime = Field(default_factory=utc_now)
    lookback_days: int
    summary: DashboardSummary
    opportunities: list[CarryOpportunity]
    venues: list[VenueStatus]


class AdapterResult(BaseModel):
    instruments: list[Instrument] = Field(default_factory=list)
    snapshots: list[MarketSnapshot] = Field(default_factory=list)
    funding: list[FundingRate] = Field(default_factory=list)
