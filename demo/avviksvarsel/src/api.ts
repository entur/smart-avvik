// Typed client for the /varsel-api endpoints (disruptions/api/varsel.py).

export interface Config {
  push_enabled: boolean;
  vapid_public_key: string | null;
  feedback_enabled?: boolean;
  // Which deployment answered: 'dev' | 'tst' on non-prod, null in prd. Optional so an older API
  // (or a local stub) simply reads as production instead of breaking the app.
  environment?: string | null;
  // Whether this deployment also sends Oslo rush-hour forecast pushes (togvarsel) to subscribed
  // devices. Optional for the same reason: an older API simply shows no notice.
  togvarsel_notify?: boolean;
}

export interface StopItem {
  stop_place: string;
  stop_name: string | null;
  mode: string; // 'Tog' | 'Buss' | 'Tog/Buss' — which families serve the stop today
  n_calls: number; // scheduled calls today — traffic signal for ranking picker matches
  place_id: string; // the physical place; refs sharing one are the same stop registered twice
  region: string | null; // county — shown when a name belongs to more than one place
}

// What has actually been observed on the line, as opposed to predicted. `observed_at` is the aimed
// time of that call — the observation can be up to an hour old (the planned source is rebuilt on a
// TTL), so it is shown with its time rather than as "now".
export interface LatestObservation {
  cancelled: boolean;
  at_stop: string | null;
  at_stop_name: string | null;
  delayed_min: number | null; // 0 when on time or early; null when not recorded
  observed_at: string | null;
}

export interface AlternativeLeg {
  mode: string | null;
  line: string | null;
  public_code: string | null;
  from_name: string | null;
  to_name: string | null;
  start_time: string | null;
  end_time: string | null;
}

// A faster way from from_stop to to_stop than the affected train, from the Journey Planner.
export interface AlternativeJourney {
  departure_time: string | null;
  arrival_time: string | null;
  duration_min: number | null;
  minutes_saved: number;
  legs: AlternativeLeg[];
}

// Every field added after `alertworthy` is optional, so a PWA build can be newer or older than the
// API it talks to without breaking during a rollout.
export interface Alert {
  situation_number: string;
  summary: string | null;
  description?: string | null; // the operator's fuller explanation
  operator: string | null;
  mode: string; // 'Tog' | 'Buss' — vehicle noun for the UI
  line: string | null;
  departure_time: string | null;
  // ET has already declared this departure cancelled: a fact, not a forecast. When true the card
  // says "Innstilt" and never a percentage — cancel_probability is 1.0 by construction.
  cancelled?: boolean;
  cancel_probability: number;
  delay_p50_min: number | null; // median delay from the exceedance stack (as /demo shows)
  delay_p90_min: number | null;
  ge10_probability: number | null;
  impact_probability: number | null; // P(innstilling eller >= 10 min forsinkelse) — the alert score
  alert_threshold: number; // the operator's cut applied to impact_probability
  alertworthy: boolean; // impact_probability >= alert_threshold, i.e. a push is/was sent
  latest_observation?: LatestObservation | null;
  alternative_journey?: AlternativeJourney | null;
  // The model-written message for this journey × situation — identical for every device on the
  // same journey, and always the same text the push carried. Null in three cases: no wording has
  // been produced yet, none could be, or this deployment writes one but delivers the deterministic
  // wording instead (VARSEL_LLM_ENABLED=false — prd ran that way until 2026-08-23 and it is still
  // the documented revert, so the state stays live). Render the fallback for null;
  // it is the ordinary state, not an error.
  message?: string | null;
  feedback_context_id?: string | null;
  feedback_enabled?: boolean;
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

export interface Journey {
  journey_id: string;
  from_stop: string;
  from_name: string | null;
  to_stop: string;
  to_name: string | null;
  begin: string;
  end: string;
  days: number[];
  created_ts: string;
  alerts: Alert[];
  // Something about the registration itself, not about today's traffic: currently only "no planned
  // trip connects these stops today", which means this journey cannot produce an alert.
  warning: string | null;
}

export interface NewJourney {
  from_stop: string;
  from_name: string | null;
  to_stop: string;
  to_name: string | null;
  begin: string;
  end: string;
  days: number[];
  // Version of the registration-visible privacy texts the user saw (personvern.ts
  // CONSENT_TEXT_VERSION), persisted on the journey record so which wording was consented to is
  // provable. Optional on the wire in both directions: an older API ignores it, and a cached
  // bundle that predates it registers without one, which reads as «pre-versioning».
  consent_text_version?: string;
}

// ---- Client -> server error reporting ---------------------------------------------------------
// Failed fetches, 5xx responses and uncaught exceptions are forwarded to POST
// /varsel-api/client-log so browser-side failures show up in the server logs. Best-effort: a
// report that cannot be sent (typically because the network is down — the very thing that made
// fetch fail) is queued and flushed after the next successful request, so even offline blips
// leave a trace once connectivity returns.

interface ClientLogEntry {
  context: string;
  message: string;
  stack?: string;
}

const pendingReports: ClientLogEntry[] = [];
const lastReportAt = new Map<string, number>();
const REPORT_THROTTLE_MS = 30_000; // per-context, so a 60s poll loop can't spam the server
const MAX_PENDING_REPORTS = 10;

async function sendReport(entry: ClientLogEntry): Promise<void> {
  try {
    await fetch('/varsel-api/client-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true, // survives tab close / backgrounding
      body: JSON.stringify({
        ...entry,
        device_id: localStorage.getItem('avviksvarsel.device_id'),
        url: location.pathname,
      }),
    });
  } catch {
    if (pendingReports.length < MAX_PENDING_REPORTS) pendingReports.push(entry);
  }
}

export function reportClientError(context: string, message: string, stack?: string): void {
  const now = Date.now();
  if (now - (lastReportAt.get(context) ?? 0) < REPORT_THROTTLE_MS) return;
  lastReportAt.set(context, now);
  void sendReport({ context, message, stack });
}

function flushPendingReports(): void {
  while (pendingReports.length > 0) void sendReport(pendingReports.shift()!);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (e) {
    // Network-level failure (offline, connection reset, server restarting) — the browser's
    // "Failed to fetch" is meaningless to the user; report the details, show Norwegian.
    reportClientError('fetch', `${init?.method ?? 'GET'} ${url}: ${String(e)}`);
    throw new Error('Får ikke kontakt med tjeneren. Sjekk nettforbindelsen og prøv igjen.');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const message = body?.detail ?? `${res.status} ${res.statusText}`;
    // 4xx are expected user-level outcomes (validation, journey cap, test-push races) — only
    // server-side failures are worth a log line.
    if (res.status >= 500) {
      reportClientError('http', `${init?.method ?? 'GET'} ${url}: ${message}`);
    }
    throw new Error(message);
  }
  flushPendingReports();
  return res.status === 204 ? (undefined as T) : res.json();
}

const jsonPost = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export function fetchConfig(): Promise<Config> {
  return request('/varsel-api/config');
}

export function fetchStops(): Promise<StopItem[]> {
  return request('/varsel-api/stops');
}

export function fetchJourneys(deviceId: string): Promise<{ journeys: Journey[] }> {
  return request(`/varsel-api/journeys?device_id=${encodeURIComponent(deviceId)}`);
}

export function createJourney(deviceId: string, journey: NewJourney): Promise<Journey> {
  return request('/varsel-api/journeys', jsonPost({ device_id: deviceId, ...journey }));
}

export function deleteJourney(journeyId: string, deviceId: string): Promise<void> {
  return request(
    `/varsel-api/journeys/${encodeURIComponent(journeyId)}?device_id=${encodeURIComponent(deviceId)}`,
    { method: 'DELETE' },
  );
}

// `brand` is what the notification is drawn in: the server has no other way to know which variant
// this install came from, and a neutral install showing the Entur mark in the notification tray is
// exactly the giveaway ?brand=none exists to avoid. An older API ignores the extra field.
export function registerDevice(
  deviceId: string,
  subscription: unknown,
  brand: string,
): Promise<void> {
  return request('/varsel-api/devices', jsonPost({ device_id: deviceId, subscription, brand }));
}

export function deleteDevice(deviceId: string): Promise<void> {
  return request(`/varsel-api/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
}

// «Slett alt»: one atomic server-side deletion of everything keyed to this device — the device
// record, every journey registered from it, and the notification-dedup entries belonging to those
// journeys. Distinct from deleteDevice, which is «Skru av» and deliberately keeps the journeys.
// Idempotent: an id the server does not know still answers 200, with journeys_deleted: 0.
export function deleteAllData(
  deviceId: string,
): Promise<{ status: string; journeys_deleted: number }> {
  return request(`/varsel-api/devices/${encodeURIComponent(deviceId)}/data`, { method: 'DELETE' });
}

export function sendTestPush(deviceId: string): Promise<{ status: string }> {
  return request('/varsel-api/test', jsonPost({ device_id: deviceId }));
}

export function submitFeedback(feedback: FeedbackSubmission): Promise<FeedbackResponse> {
  // Components may decorate a queued submission with local bookkeeping. Keep the public wire
  // contract explicit because the backend rejects unknown fields by design.
  const { context_id, response_id, event_id, revision, rating, comment, receipt } = feedback;
  return request(
    '/varsel-api/feedback',
    jsonPost({ context_id, response_id, event_id, revision, rating, comment, receipt }),
  );
}
