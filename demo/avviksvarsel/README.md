# Avviksvarsel — mobil PWA med push-varsel

A phone-first installable web app (PWA) where a traveller registers journeys
(from-stop → to-stop, a recurring daily time window and weekday selector) and gets a **Web Push
notification** when a live disruption is predicted to affect one of their departures. Tapping the
notification opens the app, which lists each registered journey and the disruptions currently
hitting it.

Scope: everything in the live per-call prediction cache — **Vy rail (NSB), Go-Ahead and the
trained bus operators** (RUT, KOL, SKY, ATB, TRO, NOR, OST, FIN, MOR). React 19 + Vite +
[Entur Linje](https://linje.entur.no) design system.

Two variants: the Entur-branded default, and an **unbranded one at `?brand=none`** — see
[Brands](#brands).

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
  by `eval/tune_alert_thresholds.py`); the in-app list shows *all* current matches, labelling the
  ones below the threshold. "Worst" is by that same impact probability, and the gate is exactly
  `/demo`'s `would_alert` — impact probability only, no cancellation-probability fallback.
- The push text quotes the **impact probability** and, when the delay drives it, the
  exceedance-stack `p50–p90` interval — the figures `/demo` shows. It deliberately does not quote
  `dep_delay_typ_min`: that raw regression reads ~2 min on boardings whose calibrated impact is
  > 90 %, which made real alerts look trivial (fixed 2026-07-27).
- Subscriptions + journeys are keyed by a client-generated `device_id` (no login — prototype).
  State persists to a GCS blob so it survives redeploys.

## Brands

`…/varsel/?brand=none` renders the app unbranded, for showing the concept to an audience that
should not read it as an Entur product. **No parameter renders Entur**, exactly as before; an
unknown slug does too.

A brand is four things, and leaving any one of them out still reads as Entur:

| | Entur (default) | `?brand=none` |
|---|---|---|
| colour | Linje as shipped | petrol/slate, `src/brands/none.css` |
| typeface | Entur Nationale | system sans |
| masthead | `<Logo/>` + "Avviksvarsel" | "Avviksvarsel" |
| tab title | Entur Avviksvarsel | Avviksvarsel |
| favicon / home screen | Entur "EN" mark | bell on petrol |
| manifest | `manifest.webmanifest` | `manifest-none.webmanifest`, `start_url=/varsel/?brand=none` |
| push notification | Entur icon, opens `/varsel/` | neutral icon, opens `/varsel/?brand=none` |

The wording of a push is deliberately **identical** across brands: it is what `/historikk` records,
and text that varied by variant would split every wording comparison along a second axis.

### How the colour override works, and why it is generated

Linje's components do **not** read the design tokens at runtime. Each `@entur/*` package's
`dist/styles.css` defines its own `--components-<pkg>-*` variables with **literal hex** (~3 100
across the packages this app imports) and its rules read those; a further ~120 declarations skip
variables altogether, 31 of them the `outline:2px solid #181c56` focus ring on every control. So
overriding `--lavender-90` reskins nothing, and a hand-written override sheet would be a guess at
which of 3 100 names matter.

`scripts/make_brand_css.py` instead walks the published stylesheets and recolours every colour
literal in **OKLCh**, keeping each colour's perceptual lightness and moving only hue and chroma —
which is what stops a reskin quietly breaking the contrast Entur tuned those ramps for. Status
hues (red/amber/green) keep their meaning; only the Entur lavender/blue band rotates to petrol.
The result is committed as `src/brands/none.generated.css`, so no build step depends on parsing
`node_modules`.

```sh
uv run python scripts/make_brand_css.py --report   # survey the palette and what each colour becomes
uv run python scripts/make_brand_css.py            # rewrite src/brands/none.generated.css
uv run python scripts/make_brand_css.py --check    # fail if the committed sheet is stale
```

**Re-run it after upgrading Linje**, and check it in — `--check` is what tells you it drifted.

Two things about the generated sheet that are easy to get wrong, both of which shipped once and
were caught in the browser rather than by a test:

- Non-root rules are scoped inside **`:where()`**, which contributes zero specificity, so each copy
  matches at exactly the weight Linje wrote it at and wins only on source order. Prefixing them
  the obvious way raises every rule by one class and **flattens Linje's own hierarchy**:
  `.eds-h1{color}` then tied with `.eds-contrast .eds-h1{color:#fff}` and won, painting the
  masthead heading the same near-black as the band behind it. Root token blocks are the exception
  — those *must* outrank `:root`, and nothing more specific competes for them.
- Linje writes **CSS fallback pairs** — `color:#181c56; color:var(--primary-text-color)` — where
  the literal is dead weight for browsers without custom properties. Emitting only the half that
  changed leaves the legacy literal last, so the copy resolves to a value no browser was ever
  meant to use. The generator emits every declaration of any property it touched.

And one class of Entur colour the generator **cannot** see, because it is not in a stylesheet at
all: `@entur/icons` bakes the transport marks' colour into the component MARKUP
(`<svg class="eds-icon__train" color="#00367f"><path fill="#00367F">`). Those leak straight through
a colour reskin — found in the stop picker, where every row carries one, and on a near-monochrome
page they are the most saturated thing on screen. `src/brands/none.css` neutralises them via
`svg.eds-icon[color]`, which matches exactly the icons that hardcode a brand colour and leaves
`currentColor` ones alone. **If a component ever looks un-reskinned, check its markup before
assuming the generator missed a rule.**

### Adding an operator brand

`brand=none` is a *removal* and needs no assets. A real operator brand is an *addition*: an entry
in `src/brand.ts`, a `Brand` in `make_brand_css.py`, an icon set, and their logo and colours —
which needs their artwork and permission to serve it. Note also that token overrides carry colour
and logo, not design language: Linje's radii, spacing and component shapes stay Linje.

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

Regenerate the PWA icons — the Entur mark for the default brand, a white bell on petrol for
`?brand=none` — after changing the glyph:

```sh
uv run --with pillow python scripts/make_icons.py
```
