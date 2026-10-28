// Typed client for /historikk-api. Hand-mirrored from the Pydantic models in
// disruptions/api/historikk.py — there is no codegen, so the two can drift; keep them in step.

/** Why a decision has no verdict. The two are different facts and must not be merged:
 *  'venter' resolves itself overnight, 'ukjent' never will. */
export type OutcomeState = 'avklart' | 'venter' | 'ukjent';

export type Verdict = 'hit' | 'false_alarm' | 'miss' | 'correct_reject';

/** One alert decision: the train, and since 2026-08-22 the endpoints of the journey it was watched
 *  for. `journey_digest` and the raw stop refs are still not served — the API resolves the refs to
 *  NSR names and pops them — but the route itself now is. See the Decision model and the PRIVACY
 *  note atop historikk_decisions.sql for what that rests on. */
export interface Decision {
  situation_number: string | null;
  summary: string | null;
  description: string | null;
  operator: string | null;
  line: string | null;
  mode: string | null;
  /** Registered journey origin, resolved to an NSR name (falls back to the bare ref). */
  from_name: string;
  /** Registered journey destination, same resolution. */
  to_name: string;
  call_date: string | null;
  departure_time: string | null;

  n_decisions: number | null;
  first_decided_at: string | null;
  /** The closest look before departure. On a decision we never alerted on this is the one that
   *  stood — the last time we scored the train and still said nothing. */
  last_decided_at: string | null;
  ever_alerted: boolean | null;
  first_alert_at: string | null;
  alert_lead_min: number | null;
  max_impact_probability: number | null;
  final_impact_probability: number | null;
  final_cancel_probability: number | null;
  final_delay_p50_min: number | null;
  final_delay_p90_min: number | null;
  alert_threshold: number | null;
  /** ET had already declared the boarding call cancelled when we decided — the probabilities are
   *  1.0 by construction. Null for decisions taken before the flag existed. */
  et_cancelled: boolean | null;
  /** A notification was ISSUED on some channel: a Web Push the push service accepted, or — since
   *  2026-09-13 — a webhook event that was minted. Read `channel` beside it: a 'both' subject can
   *  say true with no Web Push behind it. */
  pushed_any: boolean | null;
  /** /varsel registrations on this definition — the BROWSER side only. */
  n_devices: number | null;
  /** 'browser' | 'webhook' | 'both'. Null for decisions taken before webhook routes were recorded
   *  (2026-09-13), which is its own answer and not 'browser'. */
  channel: string | null;
  /** Webhook subscriptions on this definition. A count and nothing more — the decision log carries
   *  no client, subscription or destination identity. */
  n_subscriptions: number | null;
  message: string | null;
  message_source: string | null;

  actual_cancelled: boolean | null;
  actual_dep_delay_min: number | null;
  /** null means UNKNOWN, never "no trouble". */
  actual_trouble: boolean | null;
  verdict: Verdict | null;
  outcome_state: OutcomeState;
}

export interface DecisionsResponse {
  from_date: string;
  to_date: string;
  count: number;
  decisions: Decision[];
}

/** One half of the window: the same four verdicts over a subset of the same decisions. */
export interface Segment {
  n_hit: number;
  n_false_alarm: number;
  n_miss: number;
  n_correct_reject: number;
  /** Null when this segment alerted nothing — not 0, which would read as "all wrong". */
  precision: number | null;
  recall: number | null;
  /** Median minutes from first alert to the AIMED departure, over this segment's hit TRAINS — not
   *  its hit rows, which are per situation. Exact for a cancellation; understates the warning for
   *  a delayed train, which left later than aimed. */
  median_lead_min_on_hits: number | null;
  /** Per train, like the median. Its denominator is n_hit_trains, never n_hit. */
  n_hit_with_30min_notice: number;
  n_hit_trains: number;
}

/** One operating day of the blended figures. Days with no labelled decision are absent. */
export interface DailyPoint {
  call_date: string;
  n_hit: number;
  n_false_alarm: number;
  n_miss: number;
  precision: number | null;
  recall: number | null;
}

export interface Summary {
  from_date: string;
  to_date: string;
  n_hit: number;
  n_false_alarm: number;
  n_miss: number;
  n_correct_reject: number;
  /** Labelled but unresolvable. Deliberately not folded into correct_reject. */
  n_unknown: number;
  /** Too recent to have been labelled yet. */
  n_pending: number;
  precision: number | null;
  /** Among SX-flagged calls on registered journeys — not true recall. */
  recall: number | null;
  /** Per TRAIN, unlike every other figure here — see the Segment field of the same name. */
  median_lead_min_on_hits: number | null;
  n_hit_with_30min_notice: number;
  n_hit_trains: number;

  /** The headline figures above are BLENDED — forecasts, relayed ET cancellations and decisions
   *  about trains nobody published anything about, together — which is the honest answer to "can
   *  the app be trusted". These four split them, because the populations behave nothing alike: a
   *  declared cancellation is served at probability 1.0 by construction and says nothing about
   *  whether the models saw anything coming, while a no-SX decision is taken with strictly less
   *  information than either. Quoted alone the blended number moves with the mix. */
  predicted: Segment;
  relayed: Segment;
  /** No SX message named the train at all; the models read real-time data alone. Absent from
   *  responses served before 2026-08-20, hence optional. */
  no_sx?: Segment;
  /** The train is missing from the day's timetable and no cancellation was published
   *  (`KIX:PlanRemoved:`). Served at 1.0 like a relayed cancellation, but ET declared nothing.
   *  Absent from responses served before 2026-09-23, hence optional. */
  removed?: Segment;
  /** «Med bytte»: a departure that reaches the destination only with a change, judged end to end
   *  and labelled on the arrival there. Resolved ahead of every other segment. Absent from
   *  responses served before multi-leg U11, hence optional. */
  transfer?: Segment;
  /** Decided before the flag existed; in neither segment. While this dominates, the split covers
   *  only a slice of the window. */
  n_unsegmented: number;
  /** Oldest first, same rows and filters as the totals. Absent from responses served before
   *  2026-09-25, hence optional. */
  daily?: DailyPoint[];
}

/** One entry in the "Fra"/"Til" dropdowns: one physical PLACE, not one stop ref.
 *
 *  A station's rail and bus sides are separate NSR refs sharing a parent, and a registration lands
 *  on whichever one it was made against — so they are offered as a single row, and the server
 *  expands the row back to all of its refs when filtering. `ref` is just the row's identity. */
export interface PlaceOption {
  ref: string;
  /** Null when the stop registry could not be read or does not hold the ref. Show the ref then. */
  name: string | null;
  /** How many stop refs this row stands for; above 1 means co-located refs were merged. */
  n_refs: number;
}

/** What the dropdowns can be set to, over the current window.
 *
 *  Its own endpoint, not derived from the decisions on screen: that list is capped at 200 and
 *  ordered by departure, so options taken from it would omit anything that only ran early in the
 *  window — and would shrink to the current selection once a filter was applied. It is also the
 *  server's allow-list, which is why a stale selection must be cleared rather than sent: an
 *  unknown value is a 400, not an empty result. */
export interface FilterOptions {
  from_date: string;
  to_date: string;
  operators: string[];
  /** Full line refs ("VYG:Line:R11"), not the short code the cards show — two codespaces can share
   *  a short code, and the ref is what the filter compares against. Render with lineCode(). */
  lines: string[];
  from_places: PlaceOption[];
  to_places: PlaceOption[];
  /** 'browser' | 'webhook' | 'both'. A FIXED vocabulary, unlike every list above it: three values
   *  is already a bounded cache key, and a channel absent from the window is a true answer rather
   *  than an option to hide. So this one never goes stale when the window moves. */
  channels: string[];
}

/** Which deployment answered. Its own endpoint because it must survive a decision log that cannot
 *  be read — see the config route in disruptions/api/historikk.py. */
export interface Config {
  /** 'dev' | 'tst' on non-prod, null in prd. */
  environment: string | null;
}

export interface Filters {
  fromDate?: string;
  toDate?: string;
  operator?: string;
  /** Full line ref, e.g. "VYG:Line:R11". */
  line?: string;
  /** StopPlace ref the registration boards at. Values come from fetchFilterOptions — sending one
   *  the server does not know is a 400, so a selection is cleared when the window moves off it. */
  fromPlace?: string;
  /** StopPlace ref the registration alights at. */
  toPlace?: string;
  /** Which channel watched the definition: 'browser' (a /varsel registration), 'webhook' (a client
   *  subscription) or 'both'. A POPULATION filter, so it reaches the summary too. Decisions taken
   *  before 2026-09-13 carry no channel and are excluded by any value here. */
  channel?: string;
  verdict?: Verdict;
  alertedOnly?: boolean;
  /** Only decisions whose outcome is known — 'avklart', i.e. everything except 'venter' and
   *  'ukjent'. Wider than picking a single verdict, which narrows within this same population. */
  resolvedOnly?: boolean;
  /** Only decisions an operator published a SIRI-SX message about, dropping the ones this codebase
   *  minted a situation number for — the no-SX predictions and the ET-cancellation relay. Applied
   *  in the QUERY rather than over the fetched rows: the list is capped at 200, and in dev the
   *  no-SX population is most of it, so filtering client-side would show a handful of cards and
   *  look like an empty window. */
  sxOnly?: boolean;
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function query(filters: Filters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams(extra);
  if (filters.fromDate) params.set('from_date', filters.fromDate);
  if (filters.toDate) params.set('to_date', filters.toDate);
  if (filters.operator) params.set('operator', filters.operator);
  if (filters.line) params.set('line', filters.line);
  if (filters.fromPlace) params.set('from_place', filters.fromPlace);
  if (filters.toPlace) params.set('to_place', filters.toPlace);
  if (filters.channel) params.set('channel', filters.channel);
  if (filters.verdict) params.set('verdict', filters.verdict);
  if (filters.alertedOnly) params.set('alerted_only', 'true');
  if (filters.resolvedOnly) params.set('resolved_only', 'true');
  if (filters.sxOnly) params.set('sx_only', 'true');
  return params.toString();
}

export function fetchConfig(): Promise<Config> {
  return get('/historikk-api/config');
}

export function fetchDecisions(filters: Filters): Promise<DecisionsResponse> {
  return get(`/historikk-api/decisions?${query(filters, { limit: '200' })}`);
}

export function fetchSummary(filters: Filters): Promise<Summary> {
  // Only the filters that narrow the POPULATION reach the summary — which trains, on whose
  // registered journeys — so its rates describe the same slice the list beside it shows. The
  // verdict/alerted/resolved ones narrow by OUTCOME and deliberately do not: filtering to hits
  // would make precision read 100 %. The API refuses them here for the same reason.
  const { fromDate, toDate, operator, line, fromPlace, toPlace, channel } = filters;
  return get(
    `/historikk-api/summary?${query({ fromDate, toDate, operator, line, fromPlace, toPlace, channel })}`,
  );
}

/** The dropdown vocabulary for a window. Takes only the window: options narrowed by the current
 *  selection would shrink to it, leaving no way back to the rest of the window. */
export function fetchFilterOptions(
  dateWindow: Pick<Filters, 'fromDate' | 'toDate'>,
): Promise<FilterOptions> {
  return get(`/historikk-api/filters?${query(dateWindow)}`);
}
