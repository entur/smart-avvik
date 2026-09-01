// Typed client for the /togmelding-api endpoints (disruptions/api/togvarsel.py), plus the one
// endpoint this page shares with the Avviksvarsel PWA: POST /varsel-api/feedback.
//
// The feedback types below are a deliberate FORK of demo/avviksvarsel/src/api.ts rather than a
// shared package: every front-end here is built on its own, and the wire contract is small and
// pinned server-side (`extra="forbid"`). What must not drift is the endpoint and the field names —
// disruptions/api/test_togvarsel.py greps this app's sources for both.

export interface Config {
  // Which deployment answered: 'dev' | 'tst' on non-prod, null in prd. Optional so an older API
  // (or a local stub) simply reads as production instead of breaking the page.
  environment?: string | null;
}

export interface Issue {
  issue: string; // HHMM, e.g. '0700' — also its display time
  scope: string; // what window the probability is about, e.g. 'morgenrushet 07:00–09:00'
  p: number; // calibrated probability of a bad window (the trouble10 bar)
  level: string; // 'green' | 'yellow' | 'red'
  reasons: string[];
  computed_at: string;
  late: boolean; // computed well after its due time, so it saw post-cutoff reality
  feedback_context_id?: string | null;
  feedback_enabled?: boolean;
}

export interface KnownDeviation {
  summary: string | null;
  cls: string;
  planned: boolean;
  n_am_rush_journeys: number;
  n_pm_rush_journeys: number;
}

export interface Togmelding {
  date: string;
  generated_at: string | null;
  forecast_day: boolean; // false on weekends/holidays — no forecast issued
  no_forecast_reason?: string | null;
  next_issue?: string | null; // HHMM of the next issue not yet due
  overdue_issues: string[]; // past due but not issued: delayed or failed, not "not yet due"
  issues: Issue[];
  pm_outlook?: string | null;
  known_deviations: KnownDeviation[];
  skill_note: string;
}

export type FeedbackRating = 'up' | 'down';

export interface FeedbackSubmission {
  context_id: string;
  response_id: string;
  event_id: string;
  revision: number;
  rating: FeedbackRating;
  comment: string | null;
  receipt?: string | null;
}

export type FeedbackStatus =
  | 'saved'
  | 'downstream'
  | 'throttled'
  | 'expired'
  | 'disabled'
  | 'invalid_receipt';

export interface FeedbackResponse {
  status: FeedbackStatus;
  revision?: number;
  receipt?: string;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${init?.method ?? 'GET'} ${url}: ${res.status}`);
  return res.json() as Promise<T>;
}

export function fetchConfig(): Promise<Config> {
  return request('/togmelding-api/config');
}

// `cache: no-store`: the forecast changes four times a weekday and the page polls for exactly
// that, so a heuristically cached response would show a stale level with no way to tell.
export function fetchTogmelding(): Promise<Togmelding> {
  return request('/togmelding-api/today', { cache: 'no-store' });
}

export function submitFeedback(feedback: FeedbackSubmission): Promise<FeedbackResponse> {
  // Components may decorate a queued submission with local bookkeeping. Keep the public wire
  // contract explicit because the backend rejects unknown fields by design.
  const { context_id, response_id, event_id, revision, rating, comment, receipt } = feedback;
  return request('/varsel-api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ context_id, response_id, event_id, revision, rating, comment, receipt }),
  });
}
