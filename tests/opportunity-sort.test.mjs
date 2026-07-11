import assert from "node:assert/strict";
import test from "node:test";

import { sortOpportunities } from "../app/opportunity-sort.ts";

function opportunity(underlying, overrides = {}) {
  return {
    underlying,
    strategy_type: "spot_perp",
    long_venue: "synthetic_spot",
    long_symbol: `${underlying}-SPOT-ASSUMED`,
    short_venue: "lighter",
    short_symbol: underlying,
    current_carry_apr: 10,
    mean_carry_apr: 8,
    carry_apr_volatility: 4,
    positive_ratio: 0.75,
    breakeven_hours: 24,
    indicative_breakeven_hours: 18,
    history_quality: "sufficient",
    ...overrides,
  };
}

test("sorts each clickable metric in both directions", () => {
  const rows = [
    opportunity("LOW", {
      current_carry_apr: 1,
      mean_carry_apr: 2,
      carry_apr_volatility: 3,
      positive_ratio: 0.4,
    }),
    opportunity("HIGH", {
      current_carry_apr: 9,
      mean_carry_apr: 8,
      carry_apr_volatility: 7,
      positive_ratio: 0.9,
    }),
  ];

  for (const key of ["current", "mean", "volatility", "positiveRatio"]) {
    assert.deepEqual(
      sortOpportunities(rows, key, "desc").map((row) => row.underlying),
      ["HIGH", "LOW"],
    );
    assert.deepEqual(
      sortOpportunities(rows, key, "asc").map((row) => row.underlying),
      ["LOW", "HIGH"],
    );
  }
});

test("keeps missing and non-finite metrics last in either direction", () => {
  const rows = [
    opportunity("NULL", { mean_carry_apr: null }),
    opportunity("FINITE", { mean_carry_apr: 4 }),
    opportunity("NAN", { mean_carry_apr: Number.NaN }),
  ];

  for (const direction of ["desc", "asc"]) {
    const sorted = sortOpportunities(rows, "mean", direction).map(
      (row) => row.underlying,
    );
    assert.equal(sorted[0], "FINITE");
    assert.deepEqual(new Set(sorted.slice(1)), new Set(["NAN", "NULL"]));
  }
});

test("uses settled breakeven before indicative breakeven", () => {
  const rows = [
    opportunity("SETTLED", {
      breakeven_hours: 12,
      indicative_breakeven_hours: 100,
    }),
    opportunity("INDICATIVE", {
      breakeven_hours: null,
      indicative_breakeven_hours: 20,
    }),
  ];

  assert.deepEqual(
    sortOpportunities(rows, "breakeven", "asc").map(
      (row) => row.underlying,
    ),
    ["SETTLED", "INDICATIVE"],
  );
});

test("applies deterministic quality and identity tie-breakers", () => {
  const rows = [
    opportunity("BETA", { history_quality: "sufficient" }),
    opportunity("ALPHA", { history_quality: "sufficient" }),
    opportunity("OMEGA", { history_quality: "limited" }),
  ];

  assert.deepEqual(
    sortOpportunities(rows, "current", "desc").map(
      (row) => row.underlying,
    ),
    ["ALPHA", "BETA", "OMEGA"],
  );
});
