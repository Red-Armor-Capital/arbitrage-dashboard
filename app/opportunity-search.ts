export type SearchableOpportunity = {
  underlying: string;
  display_name: string | null;
  strategy_type: "perp_perp" | "spot_perp";
  long_venue: string;
  long_symbol: string;
  short_venue: string;
  short_symbol: string;
  spot_market: "US" | "KR" | "HK" | "JP" | "TW" | null;
  spot_mic?: string | null;
  spot_symbol: string | null;
};

export function matchesOpportunityQuery(
  item: SearchableOpportunity,
  query: string,
  venueLabel: (venue: string) => string,
) {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;

  const marketKeywords: Record<string, string> = {
    US: "us 美国 美股 nyse nasdaq ads",
    KR: "kr krx 韩国 韩股",
    HK: "hk hkex 香港 港股",
    JP: "jp jpx tse 日本 日股",
    TW: "tw twse 台湾 台股",
  };
  const strategyKeywords =
    item.strategy_type === "spot_perp" ? "股票 现货 永续" : "永续";
  const identityValues = [
    item.underlying,
    item.long_symbol,
    item.short_symbol,
    item.spot_symbol,
    item.spot_mic,
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
    marketKeywords[item.spot_market ?? ""] ?? "",
    strategyKeywords,
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(normalized));
}
