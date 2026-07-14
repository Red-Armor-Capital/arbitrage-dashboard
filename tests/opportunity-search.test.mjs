import assert from "node:assert/strict";
import test from "node:test";

import { matchesOpportunityQuery } from "../app/opportunity-search.ts";

const labels = {
  kr_equity: "韩股现货",
  us_equity: "美股现货",
  xyz: "trade[XYZ]",
};

function venueLabel(venue) {
  return labels[venue] ?? venue;
}

function opportunity(overrides = {}) {
  return {
    underlying: "SKHYNIX",
    display_name: "SK Hynix",
    strategy_type: "spot_perp",
    long_venue: "kr_equity",
    long_symbol: "000660.KS",
    short_venue: "xyz",
    short_symbol: "xyz:SKHX",
    spot_market: "KR",
    spot_symbol: "000660.KS",
    ...overrides,
  };
}

test("search matches Korean listing, contract symbol, and market keyword", () => {
  const row = opportunity();

  assert.equal(matchesOpportunityQuery(row, "000660.KS", venueLabel), true);
  assert.equal(matchesOpportunityQuery(row, "SKHX", venueLabel), true);
  assert.equal(matchesOpportunityQuery(row, "韩股", venueLabel), true);
  assert.equal(matchesOpportunityQuery(row, "SKHY", venueLabel), false);
});

test("search keeps US ADS row distinct", () => {
  const row = opportunity({
    underlying: "SKHY",
    display_name: "SK Hynix ADS",
    long_venue: "us_equity",
    long_symbol: "SKHY",
    short_symbol: "xyz:SKHY",
    spot_market: "US",
    spot_symbol: "SKHY",
  });

  assert.equal(matchesOpportunityQuery(row, "SKHY", venueLabel), true);
  assert.equal(matchesOpportunityQuery(row, "美股", venueLabel), true);
  assert.equal(matchesOpportunityQuery(row, "SKHX", venueLabel), false);
});
