export const DEX_VENUES = [
  "lighter",
  "extended",
  "xyz",
  "hotstuff",
  "orderly",
] as const;

export type DexVenue = (typeof DEX_VENUES)[number];

export type VenueOpportunity = {
  long_venue: string;
  short_venue: string;
};

const dexVenueSet = new Set<string>(DEX_VENUES);

export function isDexVenue(value: string): value is DexVenue {
  return dexVenueSet.has(value);
}

export function requiredDexVenues(item: VenueOpportunity): DexVenue[] {
  return [...new Set([item.long_venue, item.short_venue].filter(isDexVenue))];
}

export function matchesDexSelection(
  item: VenueOpportunity,
  selectedDexes: ReadonlySet<string>,
) {
  return requiredDexVenues(item).every((venue) => selectedDexes.has(venue));
}

export function parseDexPreference(value: string | null): Set<DexVenue> | null {
  if (value === null) return null;

  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return null;

    return new Set(
      parsed.filter(
        (venue): venue is DexVenue =>
          typeof venue === "string" && isDexVenue(venue),
      ),
    );
  } catch {
    return null;
  }
}

export function serializeDexPreference(selectedDexes: ReadonlySet<string>) {
  return JSON.stringify(
    DEX_VENUES.filter((venue) => selectedDexes.has(venue)),
  );
}
