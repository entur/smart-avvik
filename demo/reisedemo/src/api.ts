// Typed client for the /demo-api endpoints (disruptions/api/main.py).

export interface DemoStop {
  stop_place: string;
  stop_name: string | null;
  n_calls: number;
  mode: string; // 'Tog' | 'Buss' | 'Tog/Buss'
}

export interface DemoHighlight {
  situation_number: string;
  operator: string;
  mode: string; // 'Tog' | 'Buss'
  summary: string | null;
  category: string | null;
  onset: string;
  n_cancelled: number;
  n_delayed10: number;
  window_start: string;
  window_end: string;
  from_stop: string;
  from_name: string | null;
  to_stop: string;
  to_name: string | null;
}

export interface DemoMeta {
  window_start: string;
  window_end: string;
  alert_threshold: number;
  stops: DemoStop[];
  highlights: DemoHighlight[];
}

export interface DemoSnapshot {
  cancel_probability: number;
  ge10_probability: number | null;
  impact_probability: number | null;
  delay_p50_min: number | null;
  delay_p90_min: number | null;
  would_alert: boolean;
  verdict: 'hit' | 'correct_reject' | 'miss' | 'false_alarm' | null;
}

export interface DemoSide {
  stop_place: string;
  stop_name: string | null;
  scheduled_time: string;
  actual_cancelled: boolean;
  actual_delay_min: number | null;
  actual_trouble: boolean | null;
  minutes_from_onset: number | null;
  at_message: DemoSnapshot | null;
  t60: DemoSnapshot | null;
  t30: DemoSnapshot | null;
}

export interface DemoSituation {
  situation_number: string;
  summary: string | null;
  category: string | null;
  onset: string | null;
}

export interface DemoJourney {
  journey_ref: string; // operator-prefixed, e.g. 'RUT:ServiceJourney:...'
  operator: string;
  mode: string; // 'Tog' | 'Buss'
  line: string | null;
  situation: DemoSituation | null;
  departure: DemoSide;
  arrival: DemoSide;
}

export interface DemoJourneysResponse {
  from_stop: string;
  to_stop: string;
  start: string;
  end: string;
  count: number;
  journeys: DemoJourney[];
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export function fetchMeta(): Promise<DemoMeta> {
  return get('/demo-api/meta');
}

export function fetchJourneys(
  fromStop: string,
  toStop: string,
  startIso: string,
  endIso: string,
): Promise<DemoJourneysResponse> {
  const params = new URLSearchParams({
    from_stop: fromStop,
    to_stop: toStop,
    start: startIso,
    end: endIso,
  });
  return get(`/demo-api/journeys?${params}`);
}
