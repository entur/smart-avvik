// The from/to pickers' resolver: which of the ~30k served stops a typed query offers, in what
// order, and — the part that has bitten — which of them are folded into one row.
//
// A plain module with no React and no Linje in it, so `node --test` can type-strip and import it
// (the deleteAllFlow.ts / deviceIdentity.ts pattern); AddJourneyModal wraps the result in dropdown
// items and adds the mode icons.

import type { StopItem } from '../api';

// The all-operator stop list runs to ~30k entries, so the dropdowns get their items from this
// ranked resolver (prefix matches first, then word-start, then substring; busiest stops first)
// capped at MAX_SHOWN — the full list is never rendered.
export const MAX_SHOWN = 60;

function normalizedName(s: StopItem): string {
  return (s.stop_name ?? '').trim().toLowerCase();
}

// Names that belong to more than one physical place ("Sentrum", "Kirken", ~1300 of them). Those
// rows get their municipality appended (the county until 2026-09-02, which offered «Sandvika
// (Troms)» six times over — design review 2026-09-01, finding 2), because a bare name repeated in a
// list is a coin flip: picking the wrong one registers a stop the traveller's trip never calls at,
// and the journey then reports "ingen aktive avvik akkurat nå" forever. Co-located refs are already
// merged server-side, so what is left here really is several different stops sharing a name.
export function ambiguousNames(stops: StopItem[]): Set<string> {
  const places = new Map<string, Set<string>>();
  for (const s of stops) {
    const name = normalizedName(s);
    if (!name) continue;
    let seen = places.get(name);
    if (!seen) places.set(name, (seen = new Set()));
    seen.add(s.place_id ?? s.stop_place);
  }
  return new Set([...places].filter(([, refs]) => refs.size > 1).map(([name]) => name));
}

export function stopLabel(s: StopItem, ambiguous: Set<string>): string {
  const name = s.stop_name ?? s.stop_place;
  return s.region && ambiguous.has(normalizedName(s)) ? `${name} (${s.region})` : name;
}

// The server's own collapse key (main.refresh_varsel_stops), and it has to be THE SAME key: one
// row per NSR parent place AND name, an unnamed ref never merged. The server keeps different names
// under one parent apart on purpose — a parent can hold genuinely different stops (Oslo S and
// Jernbanetorget), and a station's rail side is often registered under a different name from its
// bus side. Until 2026-09-03 this de-duplicated on the place alone, which undid that decision on
// the client: «Nationaltheatret stasjon» (rail, 591 calls) and «Nationaltheatret» (bus/tram/metro,
// 1887 calls) share parent NSR:StopPlace:58404, both prefix-match the query, the busier one ranks
// first, and the train station was dropped from the list — so the picker offered Nationaltheatret
// as bus only. Measured that day on prd: 161 places serve more than one row, and three of them hid
// a rail station behind a bus-only sibling (Nationaltheatret, Ski, Oslo lufthavn; Ski's bus row
// «Ski stasjon Jernbaneveien» contains the rail name, so not even the full name reached it).
function collapseKey(s: StopItem): string {
  const name = normalizedName(s);
  return name ? `${s.place_id ?? s.stop_place} ${name}` : s.stop_place;
}

/** The stops a query offers, best match first, at most MAX_SHOWN of them. */
export function rankStops(pool: StopItem[], query: string): StopItem[] {
  const q = query.trim().toLowerCase();
  const scored: { s: StopItem; rank: number }[] = [];
  for (const s of pool) {
    const name = (s.stop_name ?? s.stop_place).toLowerCase();
    if (q === '') {
      scored.push({ s, rank: 2 });
      continue;
    }
    const idx = name.indexOf(q);
    if (idx === -1) continue;
    // prefix match > start of a later word > anywhere in the name
    const rank = idx === 0 ? 0 : name[idx - 1] === ' ' || name[idx - 1] === '(' ? 1 : 2;
    scored.push({ s, rank });
  }
  scored.sort((a, b) => a.rank - b.rank || b.s.n_calls - a.s.n_calls);
  // One row per (place, name), defensively: the server already collapses co-located refs, but an
  // older deploy — or a stop registry it could not read, which makes it fall back to one place per
  // ref — would otherwise put duplicate rows straight back into the picker. Same key as the
  // server's, see collapseKey; a looser one silently drops rows the server meant to offer.
  const items: StopItem[] = [];
  const seen = new Set<string>();
  for (const { s } of scored) {
    const key = collapseKey(s);
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(s);
    if (items.length >= MAX_SHOWN) break;
  }
  return items;
}
