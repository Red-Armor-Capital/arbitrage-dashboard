import assert from "node:assert/strict";
import test from "node:test";

import {
  DEX_VENUES,
  matchesCarrySelection,
  matchesDexSelection,
  parseDexPreference,
  requiredDexVenues,
  serializeDexPreference,
} from "../app/opportunity-filter.ts";

function opportunity(longVenue, shortVenue) {
  return {
    long_venue: longVenue,
    short_venue: shortVenue,
  };
}

test("US spot does not require an additional DEX account", () => {
  const item = opportunity("us_equity", "lighter");

  assert.deepEqual(requiredDexVenues(item), ["lighter"]);
  assert.equal(matchesDexSelection(item, new Set(["lighter"])), true);
  assert.equal(matchesDexSelection(item, new Set(["xyz"])), false);
});

test("Korean spot does not require an additional DEX account", () => {
  const item = opportunity("kr_equity", "xyz");

  assert.deepEqual(requiredDexVenues(item), ["xyz"]);
  assert.equal(matchesDexSelection(item, new Set(["xyz"])), true);
  assert.equal(matchesDexSelection(item, new Set(["lighter"])), false);
});

test("perp-perp opportunities require both DEX venues", () => {
  const item = opportunity("lighter", "xyz");

  assert.equal(
    matchesDexSelection(item, new Set(["lighter", "xyz"])),
    true,
  );
  assert.equal(matchesDexSelection(item, new Set(["lighter"])), false);
  assert.equal(matchesDexSelection(item, new Set()), false);
});

test("non-DEX venues are outside the scope of the DEX account filter", () => {
  const item = opportunity("ibkr", "binance");

  assert.deepEqual(requiredDexVenues(item), []);
  assert.equal(matchesDexSelection(item, new Set()), true);
});

test("stored preferences preserve an intentional empty selection", () => {
  assert.deepEqual([...parseDexPreference("[]")], []);
  assert.equal(parseDexPreference(null), null);
  assert.equal(parseDexPreference("not-json"), null);
});

test("stored preferences ignore unknown venues and serialize canonically", () => {
  const parsed = parseDexPreference(
    JSON.stringify(["xyz", "unknown", "lighter", "xyz"]),
  );

  assert.deepEqual([...parsed], ["xyz", "lighter"]);
  assert.equal(
    serializeDexPreference(new Set(["orderly", "lighter", "unknown"])),
    JSON.stringify(["lighter", "orderly"]),
  );
  assert.deepEqual(DEX_VENUES, [
    "lighter",
    "extended",
    "xyz",
    "hotstuff",
    "orderly",
  ]);
});

test("blank settled APR minimum does not filter current negative carry", () => {
  const historicalCandidate = {
    current_carry_apr: -12,
    mean_carry_apr: 48,
  };

  assert.equal(matchesCarrySelection(historicalCandidate, "", false), true);
  assert.equal(matchesCarrySelection(historicalCandidate, "  ", false), true);
  assert.equal(matchesCarrySelection(historicalCandidate, "0", false), true);
  assert.equal(
    matchesCarrySelection(
      { current_carry_apr: -12, mean_carry_apr: null },
      "",
      false,
    ),
    true,
  );
});

test("settled APR minimum uses history only and rejects missing history", () => {
  assert.equal(
    matchesCarrySelection(
      { current_carry_apr: 80, mean_carry_apr: null },
      "10",
      false,
    ),
    false,
  );
  assert.equal(
    matchesCarrySelection(
      { current_carry_apr: -5, mean_carry_apr: 12 },
      "10",
      false,
    ),
    true,
  );
  assert.equal(
    matchesCarrySelection(
      { current_carry_apr: 80, mean_carry_apr: 8 },
      "10",
      false,
    ),
    false,
  );
});

test("current positive carry is an independent optional filter", () => {
  assert.equal(
    matchesCarrySelection(
      { current_carry_apr: -1, mean_carry_apr: 50 },
      "",
      true,
    ),
    false,
  );
  assert.equal(
    matchesCarrySelection(
      { current_carry_apr: 1, mean_carry_apr: 50 },
      "",
      true,
    ),
    true,
  );
});
