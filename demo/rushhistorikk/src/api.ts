// Typed client for /rushhistorikk-api. Hand-mirrored from the Pydantic models in
// disruptions/api/rushhistorikk.py — there is no codegen, so the two can drift; keep them in step.
//
// Two endpoints, and that is the whole surface: /config (which deployment answered) and /days
// (the day rows and the stats table together). They come together on purpose — the rates describe
// exactly the population the list beside them shows, so the two cannot drift apart.

/** togvarsel's level vocabulary, in severity order. The forecast level, the level a standing
 *  warning leaves in force and the realized level are all this same scale. */
export type Level = 'green' | 'yellow' | 'red';

/** The four weekday issues. 0630/1200 are the baselines, 0700/1400 the follow-ups. */
export type IssueSlot = '0630' | '0700' | '1200' | '1400';

/** The stats table's row key: the blended total, or one issue slot (KD6 — the baselines push on
 *  level and the follow-ups on level CHANGE, so a blended figure moves with the mix). */
export type StatSlot = 'total' | IssueSlot;

/** Which rush an issue is about. Two issues per rush. */
export type Rush = 'am' | 'pm';

/** The verdict taxonomy, mirrored from rushhistorikk_verdicts.VERDICTS. `traff` is the right class
 *  communicated, silence included — a green forecast on a green rush is a hit. The last three are
 *  not outcomes at all — they say why there is none — and must never render as one. */
export type Verdict =
  | 'traff'
  | 'traff_nesten'
  | 'falsk_alarm'
  | 'bom'
  | 'venter'
  | 'ukjent'
  | 'ikke_vurdert';

/** What a round pushed, when it pushed. `all_clear` is the friskmelding — it RETRACTS a warning,
 *  so it is never coverage. */
export type NotifyKind = 'warning' | 'update' | 'all_clear';

/** Why a round decided not to push. `no_change` is the confirming follow-up: nothing was sent
 *  because the baseline's warning still stands, which is what `baseline_level` carries.
 *  `yellow_baseline` is legacy: a yellow baseline held back under the red-only rule, live from the
 *  evening of 2026-09-02 to 2026-09-03 (PR #130, reverted) — kept so a row from that window still
 *  reads; the live rule pushes at yellow. */
export type NotifyReason = 'untimely' | 'no_change' | 'green_baseline' | 'yellow_baseline';

/** One forecast issue: what it said, what we communicated, and how the rush turned out. */
export interface IssueRow {
  issue: IssueSlot;
  rush: Rush;
  forecast_level: Level | null;
  /** The calibrated probability behind that level. */
  p: number | null;
  /** Forecast computed more than 10 min after its own cutoff, so it saw post-cutoff reality.
   *  Excluded from Presisjon, kept in Dekning. */
  late: boolean;
  /** A notification round exists — recorded, or reconstructed from the forecast log (see
   *  `reconstructed`). False means «ikke vurdert»: none recorded and none reconstructable — never
   *  that no warning was warranted. */
  recorded: boolean;
  /** The round is not a record: no round row existed, so the push policy was replayed on the
   *  forecast at its own computed_at — what would have been decided with delivery on and on time.
   *  Graded like a recorded round (the page judges the model), counted apart, never a sent push. */
  reconstructed: boolean;
  /** A push was ISSUED — the policy decision, not delivery. A round can match zero devices and
   *  still be what the traveller would have received. */
  sent: boolean;
  kind: NotifyKind | null;
  reason: NotifyReason | null;
  /** For a follow-up, the level its baseline left in force — the level a confirming follow-up is
   *  graded on, since that is what the traveller still holds. */
  baseline_level: Level | null;
  decided_at: string | null;
  /** The realized level over THIS issue's own trained window: 0630 -> 06:30-09:00,
   *  0700 -> 07:00-09:00, 1200/1400 -> 15:00-18:00. */
  actual_level: Level | null;
  /** The share behind that level: calls cancelled OR >= 10 min late, over resolved calls. */
  actual_trouble10: number | null;
  /** The realized value is the same-day reading and can still change overnight. Excluded from the
   *  stats table, shown on the day row with a marker. */
  provisional: boolean;
  verdict: Verdict;
  /** Minutes from the push to the start of the rush it is about — positive when the push preceded
   *  it. Near zero or negative for the morning issues by construction. */
  lead_min: number | null;
  /** Whether this issue is in the population Presisjon and the confusion matrix are over. */
  scored: boolean;
}

/** One weekday: its four issues, and what the two rushes actually did. */
export interface DayRow {
  /** The Oslo date, '2026-09-01'. */
  d: string;
  /** The day's issues, 0630 first. Usually four, but a failed rebuild can leave fewer. */
  issues: IssueRow[];
  am_level: Level | null;
  am_trouble10: number | null;
  /** Realized level over 07:00-09:00 — the 0700 issue's own window, shown so the two morning
   *  figures cannot silently disagree. */
  amrest_level: Level | null;
  pm_level: Level | null;
  pm_trouble10: number | null;
  /** What the page showed at 09:20, if it differs from the settled value. */
  am_provisional_level: Level | null;
  /** What the page showed at 18:20, if it differs. */
  pm_provisional_level: Level | null;
  /** The training chain's day-level test (am and pm both above 300 calls). False means the realized
   *  share means nothing and the day is not scored; null means it is not established yet. */
  eligible: boolean | null;
}

/** One row of the 3x3: a forecast level against the three realized levels. */
export interface MatrixRow {
  forecast: Level;
  green: number;
  yellow: number;
  red: number;
}

/** One row of «Slik har varslene truffet»: the total, or one issue slot.
 *
 *  The two rates are precision and recall over NOTIFICATIONS, and over DIFFERENT populations —
 *  Presisjon over the pushes on scored issues, Dekning over the situations that owed a push, late
 *  and untimely rounds kept — which is why each carries its own count and the page states it rather
 *  than leaving one denominator to be assumed. */
export interface StatsRow {
  slot: StatSlot;
  /** Of the pushes that went out, the share that were justified: a yellow or red push on a rush
   *  that turned yellow or red (either level), or a friskmelding on a rush that turned green. Null
   *  on an empty denominator, never 0 (R17) — a calm window has no pushes to rate. */
  presisjon: number | null;
  /** Pushes behind that rate: sent issues in the scored population, reconstructed rounds included. */
  presisjon_den: number;
  /** Of the situations where a push was owed, the share that got a justified one. A situation is a
   *  rush that turned yellow or red with no warning standing, or turned green with one standing (a
   *  friskmelding was owed); a confirming follow-up is none. Per ISSUE on a slot row, per RUSH on
   *  the total row — a rush caught at the follow-up after a silent baseline is covered there, and
   *  the lateness is Varslingstid's to show. */
  dekning: number | null;
  /** Situations behind that rate — issues on a slot row, rushes on the total row — and the count
   *  most likely to be a handful. */
  dekning_den: number;
  /** Median minutes from the push to the rush's start. The last push per rush on the total row. */
  varslingstid_min: number | null;
  varslingstid_n: number;
  /** Pushes issued in the window, friskmeldinger included — one per RECORDED round that sent, over
   *  every issue, unsettled days included. The notification-volume figure. */
  varsler_sendt: number;
  /** Pushes a reconstructed round would have issued. Apart from varsler_sendt because that column
   *  says sent and these never were. */
  varsler_rekonstruert: number;
  /** Of varsler_sendt, the pushes on days whose outcome is not settled yet — in the count, not yet
   *  in the matrix. Printed as «N venter» beside the count. */
  varsler_venter: number;
  n_scored: number;
  n_venter: number;
  /** Outcome unestablishable, or the day was ineligible. */
  n_ukjent: number;
  /** No round recorded and none reconstructable. */
  n_ikke_vurdert: number;
  /** Issues whose round was reconstructed from the forecast log. Orthogonal to the buckets: a
   *  reconstructed round is scored like a recorded one. */
  n_reconstructed: number;
  /** Of those, the ones on a day the round log already covered: a LOST best-effort write or a
   *  closed gate rather than the pre-launch backlog. */
  n_reconstructed_after_start: number;
  n_late: number;
  n_untimely: number;
  /** Forecast level (rows) against realized level (columns), over this row's scored issues. */
  matrix: MatrixRow[];
}

/** One forecast level: how often a scored issue forecast at it turned into a bad rush. */
export interface CalibrationRow {
  level: Level;
  n: number;
  /** Rushes that turned yellow or red — the models' label. */
  n_bad: number;
  /** The mean probability those forecasts quoted; null when n is 0. */
  mean_p: number | null;
}

/** The window per RUSH (a day's morning or afternoon, judged by its scored baseline issue). Warned
 *  = a yellow or red forecast at the baseline, or else at the follow-up. */
export interface Coverage {
  rushes: number;
  /** Distinct days, for the per-weekday price. */
  days: number;
  bad: number;
  bad_first: number;
  bad_late: number;
  bad_missed: number;
  calm_warned: number;
  calm_quiet: number;
}

export interface DaysResponse {
  days_window: number;
  from_date: string;
  /** Newest first — the API already sorts, so the page never re-sorts. */
  days: DayRow[];
  /** The total row first, then one per issue slot. */
  stats: StatsRow[];
  /** Green, yellow, red. Optional: absent before 2026-09-28. */
  calibration?: CalibrationRow[];
  coverage?: Coverage;
  /** The earliest day a notification round was ever recorded. A reconstructed round BEFORE this is
   *  the expected pre-launch backlog; one after it can only mean a lost write or a closed gate. */
  recording_started: string | null;
}

/** Which deployment answered. Its own endpoint because it must survive a /days that 503s — see the
 *  config route in disruptions/api/rushhistorikk.py. */
export interface Config {
  /** 'dev' | 'tst' on non-prod, null in prd. */
  environment: string | null;
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export function fetchConfig(): Promise<Config> {
  return get('/rushhistorikk-api/config');
}

/** The window in days. The API allow-lists exactly the picker's four values — anything else is a
 *  400 rather than one more cache entry and one more BigQuery query on a public endpoint. */
export function fetchDays(days: number): Promise<DaysResponse> {
  return get(`/rushhistorikk-api/days?days=${days}`);
}
