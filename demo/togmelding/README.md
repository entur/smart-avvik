# Rushvarsel (`/rushvarsel`)

> The product is **Rushvarsel**; the code is `togvarsel` / `togmelding`. The 2026-09-01 rename
> (from «Togmeldingen») was display-only — the route, the API prefix, this directory, the `disruptions.togvarsel_*`
> tables, `TOGVARSEL_NOTIFY_ENABLED`, the log events and the two data discriminators all kept their
> names. Renaming the route would strand every push already sitting on a lock screen.

The rush-hour forecast for the Oslo local network, rendered from `GET /rushvarsel-api/today`
(`disruptions/api/togvarsel.py`). React + Vite + [Entur Linje]; served by the disruptions API at
`/rushvarsel`.

Four prognoses per weekday — 06:30 and 07:00 for the morning rush, 12:00 and 14:00 for the
afternoon — each a green / yellow / red level on a calibrated probability, with rule-based reasons
and a deterministic list of already-announced deviations. The follow-up of each pair replaces its
baseline on the page, so there is at most one card per rush: the newest.

## Why it looks like `/varsel`

A traveller usually arrives here by tapping a rush-forecast Web Push from the Smarte reisevarsler PWA
(`togvarsel_notify.build_payload` sends the Entur-branded install to `/rushvarsel`), and two
surfaces of one product should not look like two products. So the header band, the card language,
the type scale and the feedback widget are all `/varsel`'s, and `src/styles.css` is deliberately a
near-copy of `demo/avviksvarsel/src/styles.css`.

Two consequences worth knowing:

- **Entur-branded, always.** Unlike `/varsel` there is no `?brand=none` variant, because the
  unbranded install's pushes deliberately never link here — they keep their own `/varsel` URL.
- **Level colours are Linje validation tokens**, never literal hex: `--stroke-success` /
  `--fill-success-muted` and the warning/negative equivalents. The badge keeps dark ink on a light
  tint at all three levels; a filled swatch cannot, because Linje's canary is too light for white
  text and too light to darken without leaving the palette.

## The feedback widget

Each issue card carries «Var denne prognosen nyttig?» and posts to the **shared**
`POST /varsel-api/feedback` — the gate, rate limits, receipts and the export/review tooling are
all `/varsel`'s, unchanged. The server mints one context per issued forecast and keeps its id
byte-stable while the forecast is unchanged, which is what lets the 5-minute poll re-render a card
without discarding a half-typed comment (the widget is keyed on the context id).

`src/format.ts`'s `LEVEL_TEXT` and `levelSentence` are **mirrored** by `togvarsel._LEVEL_TEXT` and
`togvarsel._feedback_snapshot`, because the persisted snapshot has to say what the traveller
actually read. `disruptions/api/test_togvarsel.py` greps this app's sources and fails if they
drift — including the JS-vs-Python rounding of the percentage.

## The published privacy texts

The experiment disclaimer and the T7 sender footer live in `src/personvern.ts` — a **duplicate** of
`demo/avviksvarsel/src/personvern.ts`, which is the single source for every published privacy text.
This app cannot import it: the image's node stage copies only `demo/togmelding/` into its build
directory, so a relative import into `../avviksvarsel/src/` resolves on a laptop and fails in CI.
(Before 2026-09-01 the reason was different — no build step at all — and the arrangement was the
same.) `disruptions/api/test_personvern_texts.py` resolves the constants out of both modules,
asserts they are identical, asserts this app actually renders each of them, and refuses a bracketed
placeholder in either copy. **Edit both, or that suite fails.**

## Build

```sh
./build.sh          # -> disruptions/api/togmelding_static/ (gitignored)
npm run dev         # local dev; proxies /rushvarsel-api and /varsel-api to localhost:8000
```

The image builds it in the root `Dockerfile`'s node stage. Until 2026-09-01 this page was a single
hand-written HTML file committed under `disruptions/api/togmelding_static/` (the `eval_static`
pattern); moving onto Linje means npm packages, so it now follows the same generated-directory
pattern as the other four front-ends.

[Entur Linje]: https://linje.entur.no/
