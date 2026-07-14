"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  sortOpportunities,
  type OpportunitySortKey,
  type SortDirection,
} from "./opportunity-sort";
import {
  DEX_VENUES,
  matchesDexSelection,
  parseDexPreference,
  requiredDexVenues,
  serializeDexPreference,
  type DexVenue,
} from "./opportunity-filter";

type VenueStatus = {
  venue: string;
  status: "healthy" | "degraded" | "offline" | "not_configured";
  last_success_at: string | null;
  last_error: string | null;
  instruments: number;
  latency_ms: number | null;
};

type PerpLiquidity = {
  venue: string;
  symbol: string;
  volume_24h_usd: number | null;
  open_interest_usd: number | null;
};

type Opportunity = {
  underlying: string;
  display_name: string | null;
  asset_class: "stock" | "etf" | "index" | "preipo" | "basket" | "unknown";
  strategy_type: "perp_perp" | "spot_perp";
  price_assumption: "observed" | "spot_equals_perp" | "us_spot_quote";
  fee_scope: "both_legs" | "perp_leg_only";
  long_venue: string;
  long_symbol: string;
  short_venue: string;
  short_symbol: string;
  current_carry_apr: number;
  current_rate_kind: "indicative";
  mean_carry_apr: number | null;
  carry_apr_volatility: number | null;
  positive_ratio: number | null;
  round_trip_fee_pct: number;
  breakeven_hours: number | null;
  indicative_breakeven_hours: number | null;
  sample_hours: number;
  history_quality: "sufficient" | "limited" | "unavailable";
  long_funding_apr: number;
  short_funding_apr: number;
  long_liquidity: PerpLiquidity | null;
  short_liquidity: PerpLiquidity;
  cross_basis_pct: number | null;
  spot_symbol: string | null;
  spot_price_usd: number | null;
  spot_equivalent_price_usd: number | null;
  spot_units_per_perp_unit: number | null;
  perp_price_usd: number | null;
  perp_price_kind: "mark" | "index" | null;
  spot_perp_basis_pct: number | null;
  spot_quote_source: string | null;
  spot_quote_session: string | null;
  spot_quote_delayed: boolean | null;
  spot_observed_at: string | null;
  perp_observed_at: string | null;
  price_comparison_note: string | null;
  data_freshness_seconds: number | null;
  updated_at: string;
};

type Dashboard = {
  generated_at: string;
  lookback_days: number;
  summary: {
    opportunities: number;
    best_carry_apr: number | null;
    median_breakeven_hours: number | null;
    stable_opportunities: number;
    venues_healthy: number;
    venues_total: number;
  };
  opportunities: Opportunity[];
  venues: VenueStatus[];
};

type StrategyFilter = "all" | "spot_perp" | "perp_perp";

const sortLabels: Record<OpportunitySortKey, string> = {
  mean: "7D 已结算均值",
  current: "当前预测年化",
  volatility: "年化波动率",
  positiveRatio: "正 Carry 占比",
  liquidity: "24h 成交额",
  breakeven: "手续费回本",
};

const API_BASE =
  process.env.NEXT_PUBLIC_CARRY_API_URL ?? "http://localhost:8000";
const DEX_PREFERENCE_STORAGE_KEY = "equity-carry:selected-dex:v1";

const venueNames: Record<string, string> = {
  binance: "Binance",
  bitget: "Bitget",
  bybit: "Bybit",
  gate: "Gate",
  kraken: "Kraken",
  okx: "OKX",
  lighter: "Lighter",
  extended: "Extended",
  xyz: "trade[XYZ]",
  hotstuff: "Hotstuff",
  orderly: "Orderly",
  us_equity: "美股现货",
};

function venueLabel(value: string) {
  return venueNames[value] ?? value;
}

function formatApr(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

function formatUsd(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value >= 1000 ? 2 : 2,
    maximumFractionDigits: value >= 1000 ? 2 : 4,
  }).format(value);
}

function formatCompactUsd(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    minimumFractionDigits: 0,
    maximumFractionDigits: value >= 1_000_000 ? 2 : 1,
  }).format(value);
}

function LiquidityCell({ item }: { item: Opportunity }) {
  const legs = item.strategy_type === "spot_perp"
    ? [{ side: "永续", liquidity: item.short_liquidity }]
    : [
        { side: "多", liquidity: item.long_liquidity },
        { side: "空", liquidity: item.short_liquidity },
      ];

  return (
    <div
      className="liquidityStack"
      title="24h 成交额为公开报价币名义值；OI 名义按 OI × mark 估算，未包含盘口深度与实际滑点"
    >
      {legs.map(({ side, liquidity }) => (
        <div
          className={`liquidityLeg ${side === "多" ? "longLiquidityLeg" : "shortLiquidityLeg"}`}
          key={`${side}-${liquidity?.venue ?? "missing"}-${liquidity?.symbol ?? "missing"}`}
        >
          <span className="liquidityVenue">
            {side} · {liquidity ? venueLabel(liquidity.venue) : "数据缺失"}
          </span>
          <div className="liquidityMetrics">
            <span>
              <b>24h</b>
              <strong>{formatCompactUsd(liquidity?.volume_24h_usd ?? null)}</strong>
            </span>
            <span>
              <b>OI</b>
              <strong>{formatCompactUsd(liquidity?.open_interest_usd ?? null)}</strong>
            </span>
          </div>
        </div>
      ))}
      <small>成交额 / OI 名义估算</small>
    </div>
  );
}

function quoteSessionLabel(value: string | null, delayed: boolean | null) {
  const sessionNames: Record<string, string> = {
    pre: "盘前",
    regular: "盘中",
    post: "盘后",
    closed: "已收盘",
  };
  const session = sessionNames[(value ?? "").toLowerCase()] ?? value ?? "时段未知";
  return delayed ? `${session} · 延迟/回退` : session;
}

function formatHours(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "不可回本";
  if (value === 0) return "无需覆盖费用";
  if (value < 1) return `${Math.max(1, Math.round(value * 60))} 分钟`;
  if (value < 48) return `${value.toFixed(value < 10 ? 1 : 0)} 小时`;
  return `${(value / 24).toFixed(value < 240 ? 1 : 0)} 天`;
}

function freshnessLabel(value: number | null) {
  if (value === null) return "未知";
  if (value < 60) return `${Math.round(value)} 秒`;
  return `${Math.round(value / 60)} 分钟`;
}

function isStable(item: Opportunity) {
  return (
    item.history_quality === "sufficient" &&
    item.positive_ratio !== null &&
    item.mean_carry_apr !== null &&
    item.carry_apr_volatility !== null &&
    item.positive_ratio >= 0.8 &&
    item.carry_apr_volatility <= Math.max(Math.abs(item.mean_carry_apr), 1)
  );
}

type SortableHeaderProps = {
  label: string;
  sortKey: OpportunitySortKey;
  activeSortKey: OpportunitySortKey;
  direction: SortDirection;
  onSort: (sortKey: OpportunitySortKey) => void;
};

function SortableHeader({
  label,
  sortKey,
  activeSortKey,
  direction,
  onSort,
}: SortableHeaderProps) {
  const active = sortKey === activeSortKey;
  const ariaSort: "none" | "ascending" | "descending" = active
    ? direction === "asc"
      ? "ascending"
      : "descending"
    : "none";
  const nextDirection = active && direction === "desc" ? "升序" : "降序";

  return (
    <th className="sortableHeader" scope="col" aria-sort={ariaSort}>
      <button
        className={`columnSortButton ${active ? "isActive" : ""}`}
        type="button"
        onClick={() => onSort(sortKey)}
        aria-label={`${label}，${active ? (direction === "desc" ? "当前降序" : "当前升序") : "当前未排序"}，点击切换为${nextDirection}`}
      >
        <span>{label}</span>
        <span className="sortIndicator" aria-hidden="true">
          {active ? (direction === "desc" ? "↓" : "↑") : "↕"}
        </span>
      </button>
    </th>
  );
}

function DashboardSkeleton() {
  return (
    <div className="skeletonStack" aria-label="正在载入实时数据">
      <div className="skeletonCards">
        {[0, 1, 2, 3].map((item) => (
          <div className="skeletonCard" key={item} />
        ))}
      </div>
      <div className="skeletonTable" />
    </div>
  );
}

export default function Home() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [minApr, setMinApr] = useState("0");
  const [stableOnly, setStableOnly] = useState(false);
  const [sortKey, setSortKey] = useState<OpportunitySortKey>("mean");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [strategyFilter, setStrategyFilter] =
    useState<StrategyFilter>("all");
  const [selectedDexes, setSelectedDexes] = useState<Set<DexVenue>>(
    () => new Set(DEX_VENUES),
  );
  const [dexPreferenceReady, setDexPreferenceReady] = useState(false);

  const loadDashboard = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/dashboard`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`API ${response.status}`);
      const payload = (await response.json()) as Dashboard;
      setData(payload);
      setError(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法连接实时数据服务",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(loadDashboard, 0);
    const timer = window.setInterval(loadDashboard, 15_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [loadDashboard]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        const stored = parseDexPreference(
          window.localStorage.getItem(DEX_PREFERENCE_STORAGE_KEY),
        );
        if (stored !== null) setSelectedDexes(stored);
      } catch {
        // Keep the default selection when browser storage is unavailable.
      } finally {
        setDexPreferenceReady(true);
      }
    }, 0);

    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!dexPreferenceReady) return;

    try {
      window.localStorage.setItem(
        DEX_PREFERENCE_STORAGE_KEY,
        serializeDexPreference(selectedDexes),
      );
    } catch {
      // The filter remains usable when browser storage is unavailable.
    }
  }, [dexPreferenceReady, selectedDexes]);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await fetch(`${API_BASE}/api/refresh`, { method: "POST" });
      await loadDashboard();
    } finally {
      setRefreshing(false);
    }
  };

  const handleSort = (nextSortKey: OpportunitySortKey) => {
    if (nextSortKey === sortKey) {
      setSortDirection((current) => (current === "desc" ? "asc" : "desc"));
      return;
    }

    setSortKey(nextSortKey);
    setSortDirection("desc");
  };

  const handleSortSelection = (nextSortKey: OpportunitySortKey) => {
    if (nextSortKey !== sortKey) {
      setSortKey(nextSortKey);
      setSortDirection("desc");
    }
  };

  const toggleDex = (venue: DexVenue) => {
    setSelectedDexes((current) => {
      const next = new Set(current);
      if (next.has(venue)) next.delete(venue);
      else next.add(venue);
      return next;
    });
  };

  const dexOpportunityCounts = useMemo(() => {
    const counts = Object.fromEntries(
      DEX_VENUES.map((venue) => [venue, 0]),
    ) as Record<DexVenue, number>;

    for (const item of data?.opportunities ?? []) {
      for (const venue of requiredDexVenues(item)) counts[venue] += 1;
    }

    return counts;
  }, [data]);

  const rows = useMemo(() => {
    if (!data) return [];
    const threshold = Number(minApr) || 0;
    const filtered = data.opportunities.filter((item) => {
      const matchesQuery =
        !query ||
        item.underlying.toLowerCase().includes(query.toLowerCase()) ||
        venueLabel(item.long_venue).toLowerCase().includes(query.toLowerCase()) ||
        venueLabel(item.short_venue).toLowerCase().includes(query.toLowerCase()) ||
        (item.strategy_type === "spot_perp" ? "现货 永续 合成" : "永续")
          .includes(query.toLowerCase());
      const stable = isStable(item);
      const rankingApr = item.mean_carry_apr ?? item.current_carry_apr;
      return (
        matchesQuery &&
        matchesDexSelection(item, selectedDexes) &&
        (strategyFilter === "all" || item.strategy_type === strategyFilter) &&
        rankingApr >= threshold &&
        (!stableOnly || stable)
      );
    });

    return sortOpportunities(filtered, sortKey, sortDirection);
  }, [
    data,
    minApr,
    query,
    selectedDexes,
    sortDirection,
    sortKey,
    stableOnly,
    strategyFilter,
  ]);

  const generatedAt = data
    ? new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(new Date(data.generated_at))
    : "—";

  return (
    <main className="appShell">
      <header className="topbar">
        <div className="brandBlock">
          <div className="brandMark" aria-hidden="true">
            EC
          </div>
          <div>
            <div className="eyebrow">MARKET NEUTRAL RESEARCH</div>
            <h1>Equity Carry Monitor</h1>
          </div>
        </div>
        <div className="liveCluster">
          <span className={`liveDot ${error ? "isError" : ""}`} />
          <span>{error ? "数据服务异常" : "实时监控"}</span>
          <span className="divider" />
          <span className="muted">更新于 {generatedAt}</span>
          <button
            className="refreshButton"
            onClick={handleRefresh}
            disabled={refreshing}
            type="button"
          >
            {refreshing ? "同步中" : "立即刷新"}
          </button>
        </div>
      </header>

      <section className="heroRow">
        <div>
          <p className="sectionLabel">实时资金费 CARRY</p>
          <h2>找到可覆盖交易成本的稳定 Carry</h2>
          <p className="heroCopy">
            同时比较永续—永续资金费差与美股现货—链上永续的单边资金费和折溢价。
            当前 Funding 仅作下一期指示；均值、波动率和胜率只使用已结算记录。
          </p>
        </div>
        <div className="methodNote">
          <span>当前口径</span>
          <strong>空正 Funding 永续 · 多对冲腿</strong>
          <small>美股公开行情 · Funding 为 0</small>
        </div>
      </section>

      {loading ? (
        <DashboardSkeleton />
      ) : (
        <>
          <section className="metricGrid" aria-label="策略概览">
            <article className="metricCard accentCard">
              <span>最佳 7D Carry</span>
              <strong>{formatApr(data?.summary.best_carry_apr ?? null)}</strong>
              <small>全量研究候选 · 已结算 funding 均值</small>
            </article>
            <article className="metricCard">
              <span>研究候选组合</span>
              <strong>{data?.summary.opportunities ?? 0}</strong>
              <small>{rows.length} 个符合当前筛选</small>
            </article>
            <article className="metricCard">
              <span>中位回本时间</span>
              <strong>
                {formatHours(data?.summary.median_breakeven_hours ?? null)}
              </strong>
              <small>按各组合已计费用口径</small>
            </article>
            <article className="metricCard">
              <span>稳定组合</span>
              <strong>{data?.summary.stable_opportunities ?? 0}</strong>
              <small>
                数据源 {data?.summary.venues_healthy ?? 0}/
                {data?.summary.venues_total ?? 0} 健康
              </small>
            </article>
          </section>

          <section className="workspacePanel">
            <div className="panelHeader">
              <div>
                <p className="sectionLabel">OPPORTUNITY TABLE</p>
                <h3>Carry 机会</h3>
              </div>
              <div className="filterRow">
                <label className="searchBox">
                  <span>搜索</span>
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="NVDA / Lighter"
                  />
                </label>
                <label className="compactField">
                  <span>最低年化</span>
                  <input
                    type="number"
                    value={minApr}
                    onChange={(event) => setMinApr(event.target.value)}
                    min="0"
                    step="1"
                  />
                  <b>%</b>
                </label>
                <label className="compactField selectField strategyField">
                  <span>组合</span>
                  <select
                    value={strategyFilter}
                    onChange={(event) =>
                      setStrategyFilter(event.target.value as StrategyFilter)
                    }
                  >
                    <option value="all">全部</option>
                    <option value="spot_perp">美股现货—永续</option>
                    <option value="perp_perp">永续—永续</option>
                  </select>
                </label>
                <div className="sortPicker">
                  <label className="compactField selectField">
                    <span>排序</span>
                    <select
                      value={sortKey}
                      onChange={(event) =>
                        handleSortSelection(
                          event.target.value as OpportunitySortKey,
                        )
                      }
                    >
                      <option value="mean">7D 已结算均值</option>
                      <option value="current">当前预测年化</option>
                      <option value="volatility">年化波动率</option>
                      <option value="positiveRatio">正 Carry 占比</option>
                      <option value="liquidity">24h 成交额</option>
                      <option value="breakeven">手续费回本</option>
                    </select>
                  </label>
                  <button
                    className="sortDirectionButton"
                    type="button"
                    onClick={() =>
                      setSortDirection((current) =>
                        current === "desc" ? "asc" : "desc",
                      )
                    }
                    aria-label={`${sortLabels[sortKey]}当前${sortDirection === "desc" ? "降序" : "升序"}，点击切换为${sortDirection === "desc" ? "升序" : "降序"}`}
                    title={`切换为${sortDirection === "desc" ? "升序" : "降序"}`}
                  >
                    <span aria-hidden="true">
                      {sortDirection === "desc" ? "↓" : "↑"}
                    </span>
                  </button>
                </div>
                <label className="toggleLabel">
                  <input
                    type="checkbox"
                    checked={stableOnly}
                    onChange={(event) => setStableOnly(event.target.checked)}
                  />
                  <span>只看稳定</span>
                </label>
              </div>
            </div>

            <fieldset className="dexFilterBar">
              <legend className="srOnly">我的可用 DEX</legend>
              <div className="dexFilterIntro">
                <strong>我的可用 DEX</strong>
                <small>仅保留所有链上永续腿都可交易的组合</small>
              </div>
              <div className="dexOptionList">
                {DEX_VENUES.map((venue) => {
                  const checked = selectedDexes.has(venue);
                  return (
                    <label
                      className={`dexOption ${checked ? "isSelected" : ""}`}
                      key={venue}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleDex(venue)}
                      />
                      <span>{venueLabel(venue)}</span>
                      <small>{dexOpportunityCounts[venue]} 个组合</small>
                    </label>
                  );
                })}
              </div>
              <div className="dexFilterActions">
                <span aria-live="polite">
                  已选 {selectedDexes.size}/{DEX_VENUES.length}
                </span>
                <button
                  type="button"
                  onClick={() => setSelectedDexes(new Set(DEX_VENUES))}
                  disabled={selectedDexes.size === DEX_VENUES.length}
                >
                  全选
                </button>
                <button
                  type="button"
                  onClick={() => setSelectedDexes(new Set<DexVenue>())}
                  disabled={selectedDexes.size === 0}
                >
                  清空
                </button>
              </div>
              <p className="dexFilterNote">
                美股现货腿不受此筛选影响
              </p>
            </fieldset>

            <div className="assumptionBanner">
              <strong>美股现货口径</strong>
              <span>
                现货价来自公开美股行情，按报价时间与时段标注；合约折溢价 = 合约 mark/index ÷
                换算后美股价 − 1。海力士按 10 SKHY ADS = 1 普通股换算。该报价不是券商可成交
                NBBO，现货交易费、融资/机会成本、滑点和税费仍未计入。
              </span>
            </div>

            <div className="liquidityBanner">
              <strong>流动性口径</strong>
              <span>
                24h 成交额看近期活跃度，OI 名义看市场存量；两者都不等于实际可成交深度，
                本版不合成主观评分，也不把口径不一致的盘口报价混在一起比较。
              </span>
            </div>

            {error && (
              <div className="errorBanner" role="alert">
                <strong>实时后端尚未连通</strong>
                <span>
                  {error}。前端会每 15 秒自动重试，已保存的数据仍会保留在本地。
                </span>
              </div>
            )}

            <div
              className="tableScroll"
              role="region"
              aria-label="Carry 研究候选表"
              tabIndex={0}
            >
              <table>
                <caption className="srOnly">
                  美股现货—链上永续与永续—永续 Carry 研究候选
                </caption>
                <thead>
                  <tr>
                    <th scope="col">标的</th>
                    <th scope="col">Carry 来源</th>
                    <th scope="col">美股 / 合约</th>
                    <SortableHeader
                      label="永续流动性"
                      sortKey="liquidity"
                      activeSortKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableHeader
                      label="当前预测年化"
                      sortKey="current"
                      activeSortKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableHeader
                      label="7D 已结算均值"
                      sortKey="mean"
                      activeSortKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableHeader
                      label="年化波动率"
                      sortKey="volatility"
                      activeSortKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableHeader
                      label="正 Carry 占比"
                      sortKey="positiveRatio"
                      activeSortKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <th scope="col">往返手续费</th>
                    <th scope="col">手续费回本</th>
                    <th scope="col">数据</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((item) => {
                    const stable = isStable(item);
                    const hasHistory = item.mean_carry_apr !== null;
                    const sampleSufficient =
                      item.history_quality === "sufficient";
                    const shownBreakeven =
                      item.breakeven_hours ?? item.indicative_breakeven_hours;
                    return (
                      <tr
                        key={`${item.strategy_type}-${item.underlying}-${item.long_venue}-${item.long_symbol}-${item.short_venue}-${item.short_symbol}`}
                      >
                        <td>
                          <div className="assetCell">
                            <span className="assetBadge">
                              {item.underlying.slice(0, 2)}
                            </span>
                            <div>
                              <strong>{item.underlying}</strong>
                              <small>{item.display_name ?? "US Equity"}</small>
                            </div>
                          </div>
                        </td>
                        <td>
                          <span
                            className={`strategyBadge ${
                              item.strategy_type === "spot_perp"
                                ? "syntheticBadge"
                                : ""
                            }`}
                          >
                            {item.strategy_type === "spot_perp"
                              ? "美股现货—永续"
                              : "永续—永续"}
                          </span>
                          <div className="venuePair">
                            <span className="venueLeg longLeg">
                              多 {venueLabel(item.long_venue)}
                            </span>
                            <span className="pairArrow">→</span>
                            <span className="venueLeg shortLeg">
                              空 {venueLabel(item.short_venue)}
                            </span>
                          </div>
                          <small className="symbolLine">
                            {item.strategy_type === "spot_perp"
                              ? `${item.long_symbol} / ${item.short_symbol}`
                              : `${item.long_symbol} / ${item.short_symbol}`}
                          </small>
                        </td>
                        <td className="priceBasisCell">
                          {item.strategy_type === "spot_perp" ? (
                            <div className="priceBasisStack">
                              <div>
                                <span>{item.spot_symbol} 现货</span>
                                <strong>{formatUsd(item.spot_price_usd)}</strong>
                              </div>
                              <div>
                                <span>
                                  {venueLabel(item.short_venue)} {item.perp_price_kind ?? "price"}
                                </span>
                                <strong>{formatUsd(item.perp_price_usd)}</strong>
                              </div>
                              {(item.spot_units_per_perp_unit ?? 1) !== 1 && (
                                <small>
                                  {item.spot_units_per_perp_unit} ADS = 1 合约标的 · 可比价 {formatUsd(item.spot_equivalent_price_usd)}
                                </small>
                              )}
                              <span
                                className={`basisPill ${
                                  (item.spot_perp_basis_pct ?? 0) >= 0
                                    ? "premium"
                                    : "discount"
                                }`}
                              >
                                {(item.spot_perp_basis_pct ?? 0) >= 0
                                  ? "合约溢价"
                                  : "合约折价"}{" "}
                                {formatApr(Math.abs(item.spot_perp_basis_pct ?? 0), 2).replace("+", "")}
                              </span>
                              <small className="quoteMeta" title={item.price_comparison_note ?? undefined}>
                                {item.spot_quote_source ?? "行情源未知"} · {quoteSessionLabel(item.spot_quote_session, item.spot_quote_delayed)}
                                {item.price_comparison_note?.includes("quanto") ? " · KRW quanto 参考" : ""}
                              </small>
                            </div>
                          ) : (
                            <span className="notApplicable">—<small>仅现货—永续适用</small></span>
                          )}
                        </td>
                        <td className="liquidityCell">
                          <LiquidityCell item={item} />
                        </td>
                        <td className="numberCell positiveValue">
                          {formatApr(item.current_carry_apr)}
                          <small>预计 · 尚未结算</small>
                        </td>
                        <td className="numberCell">
                          <strong>{formatApr(item.mean_carry_apr)}</strong>
                          <small>
                            {hasHistory
                              ? item.strategy_type === "spot_perp"
                                ? `${item.sample_hours}h 永续已结算样本`
                                : `${item.sample_hours}h 双永续重叠样本`
                              : item.strategy_type === "spot_perp"
                                ? "等待该永续已结算样本"
                                : "等待双永续重叠样本"}
                          </small>
                        </td>
                        <td className="numberCell">
                          <span
                            className={stable ? "stableValue" : "warningValue"}
                          >
                            {item.carry_apr_volatility === null
                              ? "—"
                              : `${item.carry_apr_volatility.toFixed(1)}%`}
                          </span>
                          <small>
                            {!hasHistory
                              ? "样本不足"
                              : !sampleSufficient
                                ? "样本积累中"
                                : stable
                                  ? "稳定"
                                  : "波动偏高"}
                          </small>
                        </td>
                        <td className="numberCell">
                          {item.positive_ratio === null
                            ? "—"
                            : `${(item.positive_ratio * 100).toFixed(0)}%`}
                          <div className="ratioTrack">
                            <span
                              style={{
                                width: `${Math.min(100, (item.positive_ratio ?? 0) * 100)}%`,
                              }}
                            />
                          </div>
                        </td>
                        <td className="numberCell">
                          {item.round_trip_fee_pct.toFixed(3)}%
                          <small>
                            {item.fee_scope === "perp_leg_only"
                              ? "永续 taker × 2 · 现货成本未计"
                              : "双方 taker × 4"}
                          </small>
                        </td>
                        <td className="numberCell breakEvenCell">
                          {formatHours(shownBreakeven)}
                          <small>
                            {item.breakeven_hours !== null
                              ? "按已结算均值"
                              : "按当前预测"}
                          </small>
                        </td>
                        <td>
                          <span
                            className={`freshnessPill ${
                              (item.data_freshness_seconds ?? 999) > 90
                                ? "stale"
                                : ""
                            }`}
                          >
                            {freshnessLabel(item.data_freshness_seconds)}
                          </span>
                          <small className="sampleLine">
                            {sampleSufficient
                              ? item.strategy_type === "spot_perp"
                                ? `${item.sample_hours}h 单永续已结算`
                                : `${item.sample_hours}h 双永续重叠`
                              : item.sample_hours > 0
                                ? `${item.sample_hours}h · 样本不足`
                                : "仅当前预测"}
                          </small>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {rows.length === 0 && !error && (
              <div className="emptyState">
                <strong>
                  {selectedDexes.size === 0
                    ? "尚未选择可用 DEX"
                    : (data?.opportunities.length ?? 0) > 0
                      ? "没有符合当前筛选的组合"
                      : "正在积累实时资金费数据"}
                </strong>
                <span>
                  {selectedDexes.size === 0
                    ? "请至少勾选一个你可以交易的链上平台。"
                    : (data?.opportunities.length ?? 0) > 0
                      ? "请调整组合类型、最低年化、稳定性或搜索条件。"
                      : "正资金费链上永续需先取得对应美股报价；双永续组合需要同一标的出现在两个健康数据源。"}
                </span>
              </div>
            )}
          </section>

          <section className="statusPanel">
            <div>
              <p className="sectionLabel">DATA SOURCES</p>
              <h3>平台连接状态</h3>
            </div>
            <div className="statusGrid">
              {(data?.venues ?? []).map((venue) => (
                <article className="statusItem" key={venue.venue}>
                  <span className={`statusDot ${venue.status}`} />
                  <div>
                    <strong>{venueLabel(venue.venue)}</strong>
                    <small>
                      {venue.status === "healthy"
                        ? `${venue.instruments} 个标的 · ${Math.round(venue.latency_ms ?? 0)}ms`
                        : venue.last_error ?? "等待连接"}
                    </small>
                  </div>
                </article>
              ))}
              {(data?.venues.length ?? 0) === 0 && (
                <span className="muted">后端启动后将在这里显示平台状态。</span>
              )}
            </div>
          </section>
        </>
      )}

      <footer>
        <span>Equity Carry Monitor · Research only</span>
        <span>
          当前 Funding 会在结算前变化；公开美股行情不是券商可成交 NBBO，且未计现货成本、滑点、税费、稳定币和保证金风险。
        </span>
      </footer>
    </main>
  );
}
