// Typed client for /tilbakeblikk-api. Hand-mirrored from the Pydantic models in
// disruptions/api/tilbakeblikk.py — there is no codegen, so the two can drift; keep them in step.

/** Why a decision has no verdict. The two are different facts and must not be merged:
 *  'venter' resolves itself overnight, 'ukjent' never will. On this page's default window — today,
 *  all day — nearly every subject is 'venter', which is exactly what SummaryStrip says out loud. */
export type OutcomeState = 'avklart' | 'venter' | 'ukjent';

export type Verdict = 'hit' | 'false_alarm' | 'miss' | 'correct_reject';

/** One alert decision replayed. Describes a *train*, not a traveller: the journey definition behind
 *  it (digest, from_stop, to_stop) and the writer's input (message_input, which embeds all three in
 *  one blob) are deliberately not served — this API is public and unauthenticated.
 *
 *  /historikk's Decision field for field, with three differences:
 *
 *  - ADDED `would_send_at`, `first_in_window`, `prompt_version` — the replayed cycle, whether it was
 *    the subject's first opportunity anywhere, and which prompt wrote the row's own wording.
 *  - REMOVED `message` and `message_source`. The stored wording is not on this card: showing the
 *    pushed text beside one regenerated from a possibly different cycle would reintroduce exactly
 *    the mismatch the 2026-08-21 fix removed from /historikk, where 41 % of cards quoted text
 *    nobody received.
 *  - RE-SCOPED: every field marked WINDOW-SCOPED below is computed over the cycles inside the
 *    selected window, while `verdict` comes whole from the nightly job. `ever_alerted: false` beside
 *    a `hit` is therefore not a contradiction — it means the alerting happened outside the window. */
export interface Decision {
  situation_number: string | null;
  summary: string | null;
  description: string | null;
  operator: string | null;
  line: string | null;
  mode: string | null;
  call_date: string | null;
  departure_time: string | null;

  /** WINDOW-SCOPED: cycles that scored this train inside the window, not over its whole life. */
  n_decisions: number | null;
  /** WINDOW-SCOPED. */
  first_decided_at: string | null;
  /** WINDOW-SCOPED: the last time inside the window that we scored the train. */
  last_decided_at: string | null;
  /** WINDOW-SCOPED: whether any cycle inside the window crossed the threshold. */
  ever_alerted: boolean | null;
  /** WINDOW-SCOPED. */
  first_alert_at: string | null;
  /** WINDOW-SCOPED: departure minus the window's first alert, so it and first_alert_at beside it
   *  describe the same cycle. Not the nightly job's whole-subject lead. */
  alert_lead_min: number | null;
  /** WINDOW-SCOPED. */
  max_impact_probability: number | null;
  /** WINDOW-SCOPED: the figures at the last cycle inside the window. */
  final_impact_probability: number | null;
  final_cancel_probability: number | null;
  final_delay_p50_min: number | null;
  final_delay_p90_min: number | null;
  alert_threshold: number | null;
  /** ET had already declared the boarding call cancelled when we decided — the probabilities are
   *  1.0 by construction. Null for decisions taken before the flag existed. */
  et_cancelled: boolean | null;
  /** WINDOW-SCOPED: whether a push was delivered from a cycle inside the window. */
  pushed_any: boolean | null;
  n_devices: number | null;

  /** When the replayed message COULD have been sent: the decided_at of the first alert-worthy cycle
   *  inside the window that recorded a writer's input. Nothing was sent from this page, which is why
   *  it is `would_`. Null means no cycle in the window was replayable at all — its own state, never
   *  "no message". */
  would_send_at: string | null;
  /** Whether would_send_at is the subject's first alert-worthy cycle ANYWHERE, not merely the
   *  earliest one this window happens to contain. False means the train was already alerting before
   *  the window opened, so the card must not claim a first opportunity the window cannot support. */
  first_in_window: boolean;
  /** SHA-256[:8] of the system prompt in force when the replayed cycle's own wording was written. */
  prompt_version: string | null;

  actual_cancelled: boolean | null;
  actual_dep_delay_min: number | null;
  /** null means UNKNOWN, never "no trouble". */
  actual_trouble: boolean | null;
  /** Comes WHOLE from the nightly job — it judges the subject's entire life, not the window. */
  verdict: Verdict | null;
  outcome_state: OutcomeState;
}

/** The resolved window, echoed on every data response so the page never has to infer it.
 *
 *  Four fields for one range because two of them are what a picker needs and two are what a contract
 *  needs. `from_time`/`to_time` are the QUANTIZED times of day (15-minute grid, from rounded down,
 *  to rounded up) and are what goes back into the two time inputs; `window_start`/`window_end` are
 *  the exact half-open instants.
 *
 *  `to_time` of "00:00:00" means the FOLLOWING midnight — the whole-day default. It cannot mean
 *  anything else: a literal 00:00 end would be at or before the start, which the API rejects. */
export interface WindowFields {
  /** The Europe/Oslo calendar day the decisions were taken on, 'YYYY-MM-DD'. */
  day: string;
  /** 'HH:MM:SS', inclusive. */
  from_time: string;
  /** 'HH:MM:SS', EXCLUSIVE. '00:00:00' is the following midnight. */
  to_time: string;
  window_start: string;
  window_end: string;
}

export interface DecisionsResponse extends WindowFields {
  count: number;
  decisions: Decision[];
}

/** Why a replayed entry has the content it has. Every one of these gets its own visible state on the
 *  card — an empty quote under a badge is the one thing this page must never show, since a bound
 *  that bound and a model that refused are different facts and only one of them is worth waiting on.
 *
 *  - `ok` — the model answered.
 *  - `no_input` — no alert-worthy cycle in the window recorded a writer's input. Nothing to replay;
 *    not an error, not "no message", and no generation was attempted.
 *  - `unreadable_input` — a recorded input that would not parse.
 *  - `disabled` — no backend could be ASKED. Never means "this deployment sends the template".
 *  - `unavailable` / `error` / `rejected` / `ungrounded` — straight off llm_message.MessageResult.
 *  - `capped` / `rate_limited` / `deadline` — this request's own bounds. The subject was not
 *    attempted, and a later request picks it up, because the cache is consulted before the budget. */
export type ReplayStatus =
  | 'ok'
  | 'no_input'
  | 'unreadable_input'
  | 'disabled'
  | 'unavailable'
  | 'error'
  | 'rejected'
  | 'ungrounded'
  | 'capped'
  | 'rate_limited'
  | 'deadline';

/** What today's prompt writes for one replayed decision — and why, when it wrote nothing.
 *
 *  Aligned POSITION BY POSITION with DecisionsResponse's list: same window, same filters, same
 *  ORDER BY, same LIMIT. The four identifying fields are echoed so the page can *assert* that
 *  alignment rather than assume it — see alignMessages() in format.ts. */
export interface ReplayedMessage {
  situation_number: string | null;
  line: string | null;
  mode: string | null;
  departure_time: string | null;
  /** decided_at of the replayed cycle — when this wording could have been sent. */
  would_send_at: string | null;

  status: ReplayStatus;
  /** What today's prompt wrote, or null on every status but 'ok'. */
  message: string | null;
  /** The template wording built from the SAME stored input — the comparison arm, which is the point
   *  of the page. Present whenever an input was replayable, including when generation was bounded
   *  out, and independent of whether this deployment delivers model prose to travellers. */
  deterministic_message: string | null;
  /** The reply thrown away on 'ungrounded' and 'rejected' — a model did answer and the answer was
   *  refused. Those two are exactly what a prompt edit produces, so the page shows the text: a badge
   *  with nothing under it hides the thing being iterated on. Never delivered, never stored. */
  discarded: string | null;
  provider: string | null;
  model: string | null;

  /** The prompt in force when the ORIGINAL wording was written; null when nothing was replayable. */
  prompt_version: string | null;
  /** The prompt that wrote `message`. Differs from prompt_version exactly when the row predates the
   *  current brief — and does NOT separate a prompt change from sampling variance. */
  current_prompt_version: string;
}

export interface MessagesResponse extends WindowFields {
  count: number;
  messages: ReplayedMessage[];
}

/** One slice of the window, split on where the decision's information came from. */
export interface Segment {
  n_hit: number;
  n_false_alarm: number;
  n_miss: number;
  n_correct_reject: number;
  /** Null when this segment alerted nothing — not 0, which would read as "all wrong". */
  precision: number | null;
  recall: number | null;
  /** Median minutes from first alert to the AIMED departure, over this segment's hit TRAINS — not
   *  its hit rows, which are per situation. Comes WHOLE from the nightly job, like the verdict, so
   *  it is the subject's real first alert and not the window-scoped lead the cards show. */
  median_lead_min_on_hits: number | null;
  /** Per train, like the median. Its denominator is n_hit_trains, never n_hit. */
  n_hit_with_30min_notice: number;
  n_hit_trains: number;
}

export interface Summary extends WindowFields {
  /** Every decision subject in the window — the population the card list shows, and the denominator
   *  every rate below must be read against. n_labelled + n_unknown + n_pending. */
  n_subjects: number;
  /** How many of those carry a verdict. On a same-day window this is a few per cent, and the rates
   *  describe only that slice — which is why the page states the two counts rather than a rate. */
  n_labelled: number;
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
   *  the app be trusted". These three split them, because the populations behave nothing alike: a
   *  declared cancellation is served at probability 1.0 by construction and says nothing about
   *  whether the models saw anything coming, while a no-SX decision is taken with strictly less
   *  information than either. Quoted alone the blended number moves with the mix. */
  predicted: Segment;
  relayed: Segment;
  /** No SX message named the train at all; the models read real-time data alone. */
  no_sx: Segment;
  /** Decided before the flag existed (2026-08-05); in no segment. */
  n_unsegmented: number;
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
 *  server's allow-list, which is why a stale selection must be cleared rather than sent: an unknown
 *  value is a 400, not an empty result. */
export interface FilterOptions extends WindowFields {
  operators: string[];
  /** Full line refs ("VYG:Line:R11"), not the short code the cards show — two codespaces can share
   *  a short code, and the ref is what the filter compares against. Render with lineCode(). */
  lines: string[];
  from_places: PlaceOption[];
  to_places: PlaceOption[];
}

/** Which deployment answered. Its own endpoint because it must survive a decision log that cannot
 *  be read — see the config route in disruptions/api/tilbakeblikk.py. */
export interface Config {
  /** 'dev' | 'tst' on non-prod, null in prd. */
  environment: string | null;
}

/** Midnight, as both time inputs hold it and as the API echoes it back.
 *
 *  It is the DEFAULT at either end, and the page sends it as nothing at all: at the start because
 *  00:00 is where the day begins anyway, and at the end because the API reads an omitted `to_time`
 *  as the FOLLOWING midnight — which is what a whole-day range means and what an `<input type=time>`
 *  cannot otherwise express. Sending both would be a 400 (`to_time` must be later than `from_time`),
 *  and that refusal is deliberate on the API's side, so the mapping has to live here. */
export const MIDNIGHT = '00:00';

/** The row cap, shared by /decisions and /messages. It MUST be the same value in both calls: the
 *  two lists are aligned by position, and a different limit truncates one of them. */
export const LIST_LIMIT = 200;

export interface Filters {
  /** Oslo day, 'YYYY-MM-DD'. Omitted means today. */
  day?: string;
  /** 'HH:MM' on the 15-minute grid; MIDNIGHT means "from the start of the day". */
  fromTime?: string;
  /** 'HH:MM' on the 15-minute grid, EXCLUSIVE; MIDNIGHT means "the rest of the day". */
  toTime?: string;
  operator?: string;
  /** Full line ref, e.g. "VYG:Line:R11". */
  line?: string;
  /** StopPlace ref the registration boards at. Values come from fetchFilterOptions — sending one
   *  the server does not know is a 400, so a selection is cleared when the window moves off it. */
  fromPlace?: string;
  /** StopPlace ref the registration alights at. */
  toPlace?: string;
  verdict?: Verdict;
  alertedOnly?: boolean;
  /** Only decisions whose outcome is known — 'avklart', i.e. everything except 'venter' and
   *  'ukjent'. Wider than picking a single verdict, which narrows within this same population. */
  resolvedOnly?: boolean;
  /** Only decisions an operator published a SIRI-SX message about, dropping the ones this codebase
   *  minted a situation number for — the no-SX predictions and the ET-cancellation relay. Applied
   *  in the QUERY rather than over the fetched rows: the list is capped, and in dev the no-SX
   *  population is most of it, so filtering client-side would look like an empty window. */
  sxOnly?: boolean;
}

/** Just the window. Its own type because /filters takes only this — options narrowed by the current
 *  selection would shrink to it, leaving no way back to the rest of the window. */
export type DateWindow = Pick<Filters, 'day' | 'fromTime' | 'toTime'>;

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
  if (filters.day) params.set('day', filters.day);
  // MIDNIGHT at either end is the default and travels as an omitted parameter — see the constant.
  if (filters.fromTime && filters.fromTime !== MIDNIGHT) params.set('from_time', filters.fromTime);
  if (filters.toTime && filters.toTime !== MIDNIGHT) params.set('to_time', filters.toTime);
  if (filters.operator) params.set('operator', filters.operator);
  if (filters.line) params.set('line', filters.line);
  if (filters.fromPlace) params.set('from_place', filters.fromPlace);
  if (filters.toPlace) params.set('to_place', filters.toPlace);
  if (filters.verdict) params.set('verdict', filters.verdict);
  if (filters.alertedOnly) params.set('alerted_only', 'true');
  if (filters.resolvedOnly) params.set('resolved_only', 'true');
  if (filters.sxOnly) params.set('sx_only', 'true');
  return params.toString();
}

export function fetchConfig(): Promise<Config> {
  return get('/tilbakeblikk-api/config');
}

export function fetchDecisions(filters: Filters): Promise<DecisionsResponse> {
  return get(`/tilbakeblikk-api/decisions?${query(filters, { limit: String(LIST_LIMIT) })}`);
}

/** The regenerated wordings for the same subjects, in the same order.
 *
 *  A separate call from fetchDecisions on purpose, and made second: a Vertex outage empties the
 *  wordings and leaves every card, filter and statistic on the page intact, where one endpoint would
 *  503 the whole thing. Takes the SAME filters and the SAME limit — the alignment depends on it. */
export function fetchMessages(filters: Filters): Promise<MessagesResponse> {
  return get(`/tilbakeblikk-api/messages?${query(filters, { limit: String(LIST_LIMIT) })}`);
}

export function fetchSummary(filters: Filters): Promise<Summary> {
  // Only the filters that narrow the POPULATION reach the summary — which cycles, which trains, on
  // whose registered journeys — so its rates describe the same slice the list beside it shows. The
  // verdict/alerted/resolved ones narrow by OUTCOME and deliberately do not: filtering to hits
  // would make precision read 100 %. The API refuses them here for the same reason.
  const { day, fromTime, toTime, operator, line, fromPlace, toPlace } = filters;
  return get(
    `/tilbakeblikk-api/summary?${query({ day, fromTime, toTime, operator, line, fromPlace, toPlace })}`,
  );
}

/** The dropdown vocabulary for a window. Takes only the window: options narrowed by the current
 *  selection would shrink to it, leaving no way back to the rest of the window. */
export function fetchFilterOptions(dateWindow: DateWindow): Promise<FilterOptions> {
  return get(`/tilbakeblikk-api/filters?${query(dateWindow)}`);
}
