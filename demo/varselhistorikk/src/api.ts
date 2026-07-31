// Typed client for /historikk-api. Hand-mirrored from the Pydantic models in
// disruptions/api/historikk.py — there is no codegen, so the two can drift; keep them in step.

/** Why a decision has no verdict. The two are different facts and must not be merged:
 *  'venter' resolves itself overnight, 'ukjent' never will. */
export type OutcomeState = 'avklart' | 'venter' | 'ukjent';

export type Verdict = 'hit' | 'false_alarm' | 'miss' | 'correct_reject';

/** One alert decision. Describes a *train*, not a traveller: the journey definition behind it
 *  (digest, from_stop, to_stop) is deliberately not served, because this API is public and
 *  unauthenticated — see the Decision model in disruptions/api/historikk.py. */
export interface Decision {
  situation_number: string | null;
  summary: string | null;
  description: string | null;
  operator: string | null;
  line: string | null;
  mode: string | null;
  call_date: string | null;
  departure_time: string | null;

  n_decisions: number | null;
  ever_alerted: boolean | null;
  first_alert_at: string | null;
  alert_lead_min: number | null;
  max_impact_probability: number | null;
  final_impact_probability: number | null;
  final_cancel_probability: number | null;
  final_delay_p50_min: number | null;
  final_delay_p90_min: number | null;
  alert_threshold: number | null;
  pushed_any: boolean | null;
  n_devices: number | null;
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
  median_lead_min_on_hits: number | null;
  n_hit_with_30min_notice: number;
}

export interface Filters {
  fromDate?: string;
  toDate?: string;
  operator?: string;
  verdict?: Verdict;
  alertedOnly?: boolean;
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
  if (filters.verdict) params.set('verdict', filters.verdict);
  if (filters.alertedOnly) params.set('alerted_only', 'true');
  return params.toString();
}

export function fetchDecisions(filters: Filters): Promise<DecisionsResponse> {
  return get(`/historikk-api/decisions?${query(filters, { limit: '200' })}`);
}

export function fetchSummary(filters: Filters): Promise<Summary> {
  // The summary describes the whole window, so the verdict/alerted filters — which narrow the
  // list — deliberately do not apply to it. Filtering to hits would make precision read 100 %.
  const { fromDate, toDate, operator } = filters;
  return get(`/historikk-api/summary?${query({ fromDate, toDate, operator })}`);
}
