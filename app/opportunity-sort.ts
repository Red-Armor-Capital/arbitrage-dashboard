export type OpportunitySortKey =
  | "mean"
  | "current"
  | "volatility"
  | "positiveRatio"
  | "breakeven";

export type SortDirection = "asc" | "desc";

export type SortableOpportunity = {
  underlying: string;
  strategy_type: string;
  long_venue: string;
  long_symbol: string;
  short_venue: string;
  short_symbol: string;
  current_carry_apr: number;
  mean_carry_apr: number | null;
  carry_apr_volatility: number | null;
  positive_ratio: number | null;
  breakeven_hours: number | null;
  indicative_breakeven_hours: number | null;
  history_quality: "sufficient" | "limited" | "unavailable";
};

const metricByKey: Record<
  OpportunitySortKey,
  (item: SortableOpportunity) => number | null
> = {
  current: (item) => item.current_carry_apr,
  mean: (item) => item.mean_carry_apr,
  volatility: (item) => item.carry_apr_volatility,
  positiveRatio: (item) => item.positive_ratio,
  breakeven: (item) =>
    item.breakeven_hours ?? item.indicative_breakeven_hours,
};

const qualityRank: Record<SortableOpportunity["history_quality"], number> = {
  sufficient: 2,
  limited: 1,
  unavailable: 0,
};

function normalizedMetric(value: number | null) {
  return value !== null && Number.isFinite(value) ? value : null;
}

function compareMetric(
  left: number | null,
  right: number | null,
  direction: SortDirection,
) {
  const normalizedLeft = normalizedMetric(left);
  const normalizedRight = normalizedMetric(right);

  // Missing and non-finite values are always last, including ascending sorts.
  if (normalizedLeft === null && normalizedRight === null) return 0;
  if (normalizedLeft === null) return 1;
  if (normalizedRight === null) return -1;
  if (normalizedLeft === normalizedRight) return 0;

  return direction === "desc"
    ? normalizedRight - normalizedLeft
    : normalizedLeft - normalizedRight;
}

function compareText(left: string, right: string) {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

function compareIdentity(
  left: SortableOpportunity,
  right: SortableOpportunity,
) {
  return (
    compareText(left.underlying, right.underlying) ||
    compareText(left.strategy_type, right.strategy_type) ||
    compareText(left.long_venue, right.long_venue) ||
    compareText(left.long_symbol, right.long_symbol) ||
    compareText(left.short_venue, right.short_venue) ||
    compareText(left.short_symbol, right.short_symbol)
  );
}

export function sortOpportunities<T extends SortableOpportunity>(
  items: readonly T[],
  sortKey: OpportunitySortKey,
  direction: SortDirection,
) {
  const metric = metricByKey[sortKey];

  return [...items].sort((left, right) => {
    const primary = compareMetric(metric(left), metric(right), direction);
    if (primary !== 0) return primary;

    const quality = qualityRank[right.history_quality] - qualityRank[left.history_quality];
    return quality || compareIdentity(left, right);
  });
}
