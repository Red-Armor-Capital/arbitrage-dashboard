export type SearchableOpportunity = {
  underlying: string;
  display_name: string | null;
  strategy_type: "perp_perp" | "spot_perp";
  long_venue: string;
  long_symbol: string;
  short_venue: string;
  short_symbol: string;
  spot_market: "US" | "KR" | null;
  spot_symbol: string | null;
};

export function matchesOpportunityQuery(
  item: SearchableOpportunity,
  query: string,
  venueLabel: (venue: string) => string,
) {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;

  const marketKeywords =
    item.spot_market === "KR"
      ? "kr krx 韩国 韩股"
      : item.spot_market === "US"
        ? "us 美国 美股 ads"
        : "";
  const strategyKeywords =
    item.strategy_type === "spot_perp" ? "股票 现货 永续" : "永续";
  const identityValues = [
    item.underlying,
    item.long_symbol,
    item.short_symbol,
    item.spot_symbol,
  ].filter(Boolean);
  const matchesIdentity = identityValues.some((value) => {
    const raw = String(value).toLowerCase();
    return raw === normalized || raw.split(":").at(-1) === normalized;
  });
  if (matchesIdentity) return true;

  return [
    item.display_name,
    venueLabel(item.long_venue),
    venueLabel(item.short_venue),
    marketKeywords,
    strategyKeywords,
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(normalized));
}
