from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .models import CarryOpportunity, PerpLiquiditySnapshot
from .security_registry import (
    ContractSpotLink,
    SecuritySpec,
    get_security,
    resolve_contract_spot,
)


HOURS_PER_YEAR = 24 * 365
MIN_STABILITY_SAMPLE_HOURS = 24
MAX_CROSS_BASIS_PCT = 10.0
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


def _nonnegative_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _perp_liquidity(row: dict) -> PerpLiquiditySnapshot:
    price = _nonnegative_float(row.get("mark_price") or row.get("index_price"))
    open_interest = _nonnegative_float(row.get("open_interest"))
    open_interest_usd = (
        open_interest * price
        if open_interest is not None and price is not None and price > 0
        else None
    )
    return PerpLiquiditySnapshot(
        venue=str(row["venue"]),
        symbol=str(row["symbol"]),
        volume_24h_usd=_nonnegative_float(row.get("volume_24h")),
        open_interest_usd=open_interest_usd,
    )


def _contract_security(row: dict) -> tuple[ContractSpotLink | None, SecuritySpec | None]:
    link = resolve_contract_spot(str(row.get("venue") or ""), str(row.get("symbol") or ""))
    return link, get_security(link.security_id) if link is not None else None


def _carry_group_key(row: dict) -> tuple[str, ...]:
    """Keep exact listed securities distinct across every strategy type."""

    link, _security = _contract_security(row)
    if link is not None:
        return ("security", link.security_id)
    if (
        str(row.get("venue") or "") in CHAIN_PERP_VENUES
        and _asset_class(row) in {"stock", "etf"}
    ):
        # A stock-like DEX contract without a reviewed link cannot be paired by
        # company name: it may be an ADR, a local share, or a different class.
        return (
            "unmapped",
            str(row.get("venue") or ""),
            str(row.get("symbol") or ""),
        )
    return ("underlying", str(row.get("underlying") or ""))


def build_carry_opportunities(
    current_rows: list[dict],
    settled_rows: list[dict],
    lookback_days: int,
    spot_rows: list[dict] | None = None,
) -> list[CarryOpportunity]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    histories = _history_by_instrument(settled_rows)
    grouped_rows: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row in current_rows:
        interval = float(row["funding_interval_hours"] or 0)
        rate = row["funding_rate"]
        if interval <= 0 or rate is None:
            continue
        row = dict(row)
        row["hourly_rate"] = float(rate) / interval
        grouped_rows[_carry_group_key(row)].append(row)

    spots_by_security_id = {
        str(row["security_id"]): row
        for row in (spot_rows or [])
        if row.get("security_id") and row.get("quote_valid") is True
    }

    opportunities: list[CarryOpportunity] = []
    for _group_key, rows in grouped_rows.items():
        _mapped_link, mapped_security = _contract_security(rows[0])
        underlying = (
            mapped_security.underlying
            if mapped_security is not None
            else str(rows[0].get("underlying") or "")
        )
        # Spot/perp carry requires a reviewed exact contract/security link. A
        # matching company name, ticker fragment, ADR, or other listing never
        # acts as a fallback.
        for short_row in rows:
            current_hourly = short_row["hourly_rate"]
            spot_link, spot_spec = _contract_security(short_row)
            asset_class = spot_spec.asset_class if spot_spec else _asset_class(short_row)
            spot_row = (
                spots_by_security_id.get(spot_link.security_id)
                if spot_link is not None
                else None
            )
            if (
                short_row["venue"] not in CHAIN_PERP_VENUES
                or short_row.get("spot_carry_eligible") is not True
                or short_row.get("force_reduce_only") is True
                or asset_class not in {"stock", "etf"}
                or spot_link is None
                or spot_spec is None
                or spot_row is None
                or spot_link.comparison_kind == "local_currency"
                or str(spot_row.get("venue") or "") != spot_spec.spot_venue
                or str(spot_row.get("symbol") or "").upper()
                != spot_spec.ticker.upper()
            ):
                continue

            spot_price_value = spot_row.get("mark_price") or spot_row.get("index_price")
            if not spot_price_value or float(spot_price_value) <= 0:
                continue
            if short_row.get("mark_price") and float(short_row["mark_price"]) > 0:
                perp_price = float(short_row["mark_price"])
                perp_price_kind = "mark"
            elif short_row.get("index_price") and float(short_row["index_price"]) > 0:
                perp_price = float(short_row["index_price"])
                perp_price_kind = "index"
            else:
                continue
            spot_price = float(spot_price_value)
            spot_equivalent = spot_price * spot_link.spot_units_per_perp_unit
            if spot_equivalent <= 0:
                continue
            # Contract-relative-to-exact-spot basis. Positive means the contract is
            # at a premium; negative means it is at a discount.
            spot_perp_basis = (perp_price / spot_equivalent - 1) * 100

            short_hist = _hourly_series(
                histories.get((short_row["venue"], short_row["symbol"]), {}), start, now
            )
            carry_hourly = [short_hist[hour] for hour in sorted(short_hist)]
            mean_apr, volatility, positive_ratio, mean_hourly, history_quality = (
                _historical_stats(carry_hourly)
            )
            # The quote is an informational market-data reference, not a configured
            # broker execution leg. Spot-side costs remain out of scope.
            round_trip_fee = 2 * float(short_row["taker_fee"] or 0)

            spot_observed_at = (
                spot_row.get("source_observed_at") or spot_row["observed_at"]
            )
            perp_observed_at = (
                short_row.get("source_observed_at") or short_row["observed_at"]
            )
            oldest = min(spot_observed_at, perp_observed_at)
            comparison_parts = [
                spot_link.comparison_note
                or f"{spot_spec.display_name} ({spot_spec.ticker})",
                (
                    f"1 contract unit = {spot_link.spot_units_per_perp_unit:g} "
                    "listed spot unit"
                ),
            ]
            if spot_spec.local_currency != "USD":
                comparison_parts.append(
                    f"{spot_spec.local_currency} spot converted with "
                    f"USD/{spot_spec.local_currency}; public FX is a reference"
                )
            comparison_note = "; ".join(comparison_parts)

            opportunities.append(
                CarryOpportunity(
                    underlying=underlying,
                    display_name=spot_spec.display_name,
                    asset_class=asset_class,
                    strategy_type="spot_perp",
                    price_assumption="spot_quote",
                    fee_scope="perp_leg_only",
                    long_venue=spot_spec.spot_venue,
                    long_symbol=spot_spec.ticker,
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
                    short_liquidity=_perp_liquidity(short_row),
                    spot_market=spot_spec.market,
                    spot_security_id=spot_spec.security_id,
                    spot_mic=spot_spec.mic,
                    spot_symbol=spot_spec.ticker,
                    spot_price_local=_nonnegative_float(
                        spot_row.get("local_price")
                    ),
                    spot_currency=spot_spec.local_currency,
                    spot_local_per_usd=_nonnegative_float(
                        spot_row.get("local_per_usd")
                    ),
                    spot_fx_symbol=spot_row.get("fx_symbol") or spot_spec.fx_symbol,
                    spot_price_usd=spot_price,
                    spot_equivalent_price_usd=spot_equivalent,
                    spot_units_per_perp_unit=spot_link.spot_units_per_perp_unit,
                    perp_price_usd=perp_price,
                    perp_price_kind=perp_price_kind,
                    spot_perp_basis_pct=spot_perp_basis,
                    spot_quote_source=spot_row.get("quote_source"),
                    spot_quote_session=spot_row.get("quote_session"),
                    spot_quote_delayed=spot_row.get("quote_delayed") is True,
                    spot_observed_at=spot_observed_at,
                    perp_observed_at=perp_observed_at,
                    price_comparison_note=comparison_note,
                    data_freshness_seconds=_freshness_seconds(oldest, now),
                )
            )

        if len(rows) < 2:
            continue
        for short_row in rows:
            for long_row in rows:
                if short_row["venue"] == long_row["venue"]:
                    continue
                if (
                    short_row.get("force_reduce_only") is True
                    or long_row.get("force_reduce_only") is True
                ):
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
                oldest = min(
                    short_row.get("source_observed_at") or short_row["observed_at"],
                    long_row.get("source_observed_at") or long_row["observed_at"],
                )
                freshness = _freshness_seconds(oldest, now)

                opportunities.append(
                    CarryOpportunity(
                        underlying=underlying,
                        display_name=(
                            mapped_security.display_name
                            if mapped_security is not None
                            else short_row.get("display_name")
                            or long_row.get("display_name")
                        ),
                        asset_class=(
                            mapped_security.asset_class
                            if mapped_security is not None
                            else _asset_class(short_row)
                        ),
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
                        long_liquidity=_perp_liquidity(long_row),
                        short_liquidity=_perp_liquidity(short_row),
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
