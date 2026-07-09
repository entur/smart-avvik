// Typed client for the /varsel-api endpoints (disruptions/api/varsel.py).

export interface Config {
  push_enabled: boolean;
  vapid_public_key: string | null;
}

export interface StopItem {
  stop_place: string;
  stop_name: string | null;
  mode: string; // 'Tog' | 'Buss' | 'Tog/Buss' — which families serve the stop today
  n_calls: number; // scheduled calls today — traffic signal for ranking picker matches
}

export interface Alert {
  situation_number: string;
  summary: string | null;
  operator: string | null;
  mode: string; // 'Tog' | 'Buss' — vehicle noun for the UI
  line: string | null;
  departure_time: string | null;
  cancel_probability: number;
  delay_typ_min: number | null;
  delay_p90_min: number | null;
  impact_probability: number | null;
  alertworthy: boolean;
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
}

export interface NewJourney {
  from_stop: string;
  from_name: string | null;
  to_stop: string;
  to_name: string | null;
  begin: string;
  end: string;
  days: number[];
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

export function registerDevice(deviceId: string, subscription: unknown): Promise<void> {
  return request('/varsel-api/devices', jsonPost({ device_id: deviceId, subscription }));
}

export function deleteDevice(deviceId: string): Promise<void> {
  return request(`/varsel-api/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
}

export function sendTestPush(deviceId: string): Promise<{ status: string }> {
  return request('/varsel-api/test', jsonPost({ device_id: deviceId }));
}
