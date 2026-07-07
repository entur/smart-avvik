# Avviksvarsel — mobil PWA med push-varsel

A phone-first installable web app (PWA) where a traveller registers journeys
(from-stop → to-stop, a recurring daily time window and weekday selector) and gets a **Web Push
notification** when a live disruption is predicted to affect one of their departures. Tapping the
notification opens the app, which lists each registered journey and the disruptions currently
hitting it.

Scope: everything in the live per-call prediction cache — **Vy rail (NSB), Go-Ahead and the
trained bus operators** (RUT, KOL, SKY, ATB, TRO, NOR, OST, FIN, MOR). React 19 + Vite +
[Entur Linje](https://linje.entur.no) design system.

Served by the disruptions API at **`/varsel`**; data + push endpoints are **`/varsel-api`**
(implemented in `disruptions/api/varsel.py`). This directory builds into
`disruptions/api/varsel_static/` — the same generated-directory pattern as `demo/reisedemo`.

## How it works

- The API's poller (~1/min) refreshes the per-call prediction cache. After each refresh,
  `varsel.check_and_notify()` matches every registered journey against the live situations and
  sends a push for any newly alert-worthy hit.
- A journey **matches** a situation when it flags a departure from the from-stop inside the
  journey's weekday + time window that later calls at the to-stop (direction check). A match is
  **pushed** only when the worst matching departure crosses the operator's alert threshold
  (`P(cancelled OR ≥10 min late)` ≥ 0.65 by default; per-operator cuts tuned at ~90 % precision
  by `eval/tune_alert_thresholds.py`); the in-app list shows *all* current matches.
- Subscriptions + journeys are keyed by a client-generated `device_id` (no login — prototype).
  State persists to a GCS blob so it survives Cloud Run redeploys.

## Install on a phone

**iPhone (Safari, iOS ≥ 16.4 — required for push):**
1. Open the service URL `…/varsel/` in Safari.
2. Tap the **Del** (Share) button, then **«Legg til på Hjem-skjerm»**.
3. Open the app **from the Home-Screen icon** (push only works when launched standalone).
4. Tap **«Slå på varsler»** and allow notifications. Use **«Send testvarsel»** to confirm.

**Android (Chrome):**
1. Open `…/varsel/` in Chrome.
2. From the menu choose **«Installer app»** / **«Legg til på startskjerm»** (or accept the prompt).
3. Open the app, tap **«Slå på varsler»** and allow notifications; **«Send testvarsel»** to confirm.

## Local development

```sh
# 1. Backend (from disruptions/api/), with push deps + a throwaway VAPID keypair for testing:
eval "$(uv run --with pywebpush python gen_vapid.py)"      # sets VAPID_* in the shell
export VAPID_PRIVATE_KEY VAPID_PUBLIC_KEY VAPID_SUBJECT
uv run --with pywebpush --with google-cloud-storage uvicorn main:app --reload --port 8000

# 2. Frontend (from demo/avviksvarsel/):
npm install
npm run dev            # Vite dev server, proxying /varsel-api -> localhost:8000
```

Push requires a secure context; `localhost` counts, so end-to-end push works locally in Chrome.
Or build once (`npm run build`) and open `http://localhost:8000/varsel/` — the API serves the
built app, mirroring production exactly.

Regenerate the PWA icons (white bell on Entur Lavender) after changing the glyph:

```sh
uv run --with pillow python scripts/make_icons.py
```
