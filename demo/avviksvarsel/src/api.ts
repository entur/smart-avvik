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

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
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
