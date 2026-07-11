from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .models import CarryOpportunity


HOURS_PER_YEAR = 24 * 365
MIN_STABILITY_SAMPLE_HOURS = 24
MAX_CROSS_BASIS_PCT = 10.0
SYNTHETIC_SPOT_VENUE = "synthetic_spot"
CHAIN_PERP_VENUES = frozenset({"lighter", "extended", "xyz", "hotstuff", "orderly"})
ASSET_CLASSES = frozenset({"stock", "etf", "index", "preipo", "basket", "unknown"})


def _hour_floor(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _history_by_instrument(rows: list[dict]) -> dict[tuple[str, str], dict[datetime, float]]:
    grouped: dict[tuple[str, str], dict[datetime, float]] = defaultdict(dict)
    for row in rows:
        interval = float(row["interval_hours"] or 0)
        if interval <= 0:
            continue
        hourly_rate = float(row["rate"]) / interval
        settled_at = _hour_floor(row["effective_at"])
        # A discrete settlement at T pays for the interval immediately preceding T.
        # Expanding it backwards avoids leaking a stale rate into the next period.
        for offset in range(1, max(1, math.ceil(interval)) + 1):
            grouped[(row["venue"], row["symbol"])][
                settled_at - timedelta(hours=offset)
            ] = hourly_rate
    return grouped


def _hourly_series(
    values: dict[datetime, float],
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    return {
        hour: rate
        for hour, rate in values.items()
        if _hour_floor(start) <= hour <= _hour_floor(end)
    }


def _historical_stats(
    carry_hourly: list[float],
) -> tuple[float | None, float | None, float | None, float | None, str]:
    if not carry_hourly:
        # The pre-settlement rate is indicative and may still change. Never
        # manufacture historical stability statistics from that estimate.
        return None, None, None, None, "unavailable"

    apr_samples = [value * HOURS_PER_YEAR * 100 for value in carry_hourly]
    return (
        statistics.fmean(apr_samples),
        statistics.pstdev(apr_samples) if len(apr_samples) > 1 else 0.0,
        sum(value > 0 for value in carry_hourly) / len(carry_hourly),
        statistics.fmean(carry_hourly),
        "sufficient" if len(carry_hourly) >= MIN_STABILITY_SAMPLE_HOURS else "limited",
    )


def _breakeven_hours(fee: float, hourly_carry: float | None) -> float | None:
    if hourly_carry is None or hourly_carry <= 0:
        return None
    value = fee / hourly_carry
    return value if math.isfinite(value) and value >= 0 else None


def _freshness_seconds(observed_at: datetime, now: datetime) -> float:
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed_at.astimezone(timezone.utc)).total_seconds())


def _asset_class(row: dict) -> str:
    value = str(row.get("asset_class") or "unknown").lower()
    return value if value in ASSET_CLASSES else "unknown"


def build_carry_opportunities(
    current_rows: list[dict],
    settled_rows: list[dict],
    lookback_days: int,
) -> list[CarryOpportunity]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    histories = _history_by_instrument(settled_rows)
    by_underlying: dict[str, list[dict]] = defaultdict(list)
    for row in current_rows:
        interval = float(row["funding_interval_hours"] or 0)
        rate = row["funding_rate"]
        if interval <= 0 or rate is None:
            continue
        row = dict(row)
        row["hourly_rate"] = float(rate) / interval
        by_underlying[row["underlying"]].append(row)

    opportunities: list[CarryOpportunity] = []
    for underlying, rows in by_underlying.items():
        # Research-only spot/perp carry. There is no live broker quote yet: the
        # synthetic spot leg is explicitly assumed to equal this chain perp's
        # mark/index price and to have zero funding. Only positive-funding perps
        # produce the long-spot / short-perp direction requested by the strategy.
        for short_row in rows:
            current_hourly = short_row["hourly_rate"]
            asset_class = _asset_class(short_row)
            if (
                short_row["venue"] not in CHAIN_PERP_VENUES
                or current_hourly <= 0
                or short_row.get("spot_carry_eligible") is not True
                or asset_class not in {"stock", "etf"}
            ):
                continue

            short_hist = _hourly_series(
                histories.get((short_row["venue"], short_row["symbol"]), {}), start, now
            )
            carry_hourly = [short_hist[hour] for hour in sorted(short_hist)]
            mean_apr, volatility, positive_ratio, mean_hourly, history_quality = (
                _historical_stats(carry_hourly)
            )
            # The assumed spot leg has no configured broker, so its commissions,
            # financing/opportunity cost, slippage and overnight fees remain out
            # of scope. We still count opening and closing the perp at taker rates.
            round_trip_fee = 2 * float(short_row["taker_fee"] or 0)

            opportunities.append(
                CarryOpportunity(
                    underlying=underlying,
                    display_name=short_row.get("display_name"),
                    asset_class=asset_class,
                    strategy_type="spot_perp",
                    price_assumption="spot_equals_perp",
                    fee_scope="perp_leg_only",
                    long_venue=SYNTHETIC_SPOT_VENUE,
                    long_symbol=underlying,
                    short_venue=short_row["venue"],
                    short_symbol=short_row["symbol"],
                    current_carry_apr=current_hourly * HOURS_PER_YEAR * 100,
                    mean_carry_apr=mean_apr,
                    carry_apr_volatility=volatility,
                    positive_ratio=positive_ratio,
                    round_trip_fee_pct=round_trip_fee * 100,
                    breakeven_hours=_breakeven_hours(round_trip_fee, mean_hourly),
                    indicative_breakeven_hours=_breakeven_hours(
                        round_trip_fee, current_hourly
                    ),
                    sample_hours=len(carry_hourly),
                    history_quality=history_quality,
                    long_funding_apr=0.0,
                    short_funding_apr=current_hourly * HOURS_PER_YEAR * 100,
                    cross_basis_pct=0.0,
                    data_freshness_seconds=_freshness_seconds(
                        short_row["observed_at"], now
                    ),
                )
            )

        if len(rows) < 2:
            continue
        for short_row in rows:
            for long_row in rows:
                if short_row["venue"] == long_row["venue"]:
                    continue
                current_hourly = short_row["hourly_rate"] - long_row["hourly_rate"]
                if current_hourly <= 0:
                    continue

                short_hist = _hourly_series(
                    histories.get((short_row["venue"], short_row["symbol"]), {}), start, now
                )
                long_hist = _hourly_series(
                    histories.get((long_row["venue"], long_row["symbol"]), {}), start, now
                )
                common_hours = sorted(set(short_hist) & set(long_hist))
                carry_hourly = [short_hist[hour] - long_hist[hour] for hour in common_hours]
                mean_apr, volatility, positive_ratio, mean_hourly, history_quality = (
                    _historical_stats(carry_hourly)
                )

                round_trip_fee = 2 * (
                    float(short_row["taker_fee"] or 0) + float(long_row["taker_fee"] or 0)
                )
                breakeven = _breakeven_hours(round_trip_fee, mean_hourly)
                indicative_breakeven = _breakeven_hours(round_trip_fee, current_hourly)

                short_price = short_row["mark_price"] or short_row["index_price"]
                long_price = long_row["mark_price"] or long_row["index_price"]
                cross_basis = None
                if short_price and long_price:
                    midpoint = (float(short_price) + float(long_price)) / 2
                    if midpoint:
                        cross_basis = (float(short_price) - float(long_price)) / midpoint * 100
                if cross_basis is not None and abs(cross_basis) > MAX_CROSS_BASIS_PCT:
                    # A large mark-price mismatch usually means a multiplier, currency,
                    # tokenisation, or symbol-mapping mismatch rather than equal delta.
                    continue

                # A pair is only as fresh as its older real leg.
                oldest = min(short_row["observed_at"], long_row["observed_at"])
                freshness = _freshness_seconds(oldest, now)

                opportunities.append(
                    CarryOpportunity(
                        underlying=underlying,
                        display_name=short_row.get("display_name") or long_row.get("display_name"),
                        asset_class=_asset_class(short_row),
                        strategy_type="perp_perp",
                        price_assumption="observed",
                        fee_scope="both_legs",
                        long_venue=long_row["venue"],
                        long_symbol=long_row["symbol"],
                        short_venue=short_row["venue"],
                        short_symbol=short_row["symbol"],
                        current_carry_apr=current_hourly * HOURS_PER_YEAR * 100,
                        mean_carry_apr=mean_apr,
                        carry_apr_volatility=volatility,
                        positive_ratio=positive_ratio,
                        round_trip_fee_pct=round_trip_fee * 100,
                        breakeven_hours=breakeven,
                        indicative_breakeven_hours=indicative_breakeven,
                        sample_hours=len(carry_hourly),
                        history_quality=history_quality,
                        long_funding_apr=long_row["hourly_rate"] * HOURS_PER_YEAR * 100,
                        short_funding_apr=short_row["hourly_rate"] * HOURS_PER_YEAR * 100,
                        cross_basis_pct=cross_basis,
                        data_freshness_seconds=freshness,
                    )
                )

    opportunities.sort(
        key=lambda item: (
            {"sufficient": 2, "limited": 1, "unavailable": 0}[item.history_quality],
            item.mean_carry_apr if item.mean_carry_apr is not None else item.current_carry_apr,
            item.current_carry_apr,
            -(item.carry_apr_volatility or 0),
        ),
        reverse=True,
    )
    return opportunities
