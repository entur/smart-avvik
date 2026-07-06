# Reisedemo — prediksjoner mot fasit

Demoside for `/disruptions`-API-et: viser hvor godt modellene predikerer forsinkelser og
innstillinger for en passasjerreise. Velg fra-/til-stasjon og tidsrom (eller et
ferdigplukket eksempelscenario), og siden viser alle matchende reiser med

- prediksjonene slik de sto **da SX-meldingen kom**, **60 min før** og **30 min før**
  toget (hver med kalibrering for sin spørrehorisont),
- **faktisk utfall** (innstilt / forsinkelse i minutter), og
- en ✓/✕-indikator for om varselsbeslutningen (terskel 0,65 på
  P(innstilt eller ≥ 10 min forsinket)) traff.

Avgang vises til venstre, ankomst til høyre. Bevisst nøytral formgivning — demoen vises
for partnere med egne designspråk.

## Data

Statisk snapshot av modellenes temporale holdout (NSB-tog, 1. apr – 20. jun 2026): alle
tall er utenfor treningsdataene. Bygges av `data/queries/create_demo_predictions.sql`
(tabellene `disruptions.demo_predictions`, `demo_calls`, `demo_stops`,
`demo_highlights`), som gjenbruker `leadtime_scored` + per-horisont-platt-lagene og
speiler serveringsmatematikken i `disruptions/api/predict_calls.sql`. Kjør spørringen på
nytt etter modell-/kalibreringsretrening for å oppdatere demoen.

## Kjøre lokalt

```sh
# backend (serverer /demo-api mot BigQuery under din ADC)
cd disruptions/api && uv run uvicorn main:app --port 8000

# frontend med hot reload (proxyer /demo-api til :8000)
cd demo/reisedemo && npm install && npm run dev
```

## Bygge og deploye

`./build.sh` bygger til `disruptions/api/demo_static/` (generert katalog, ikke i git);
API-et serverer den på `/demo`. `disruptions/api/deploy.sh` kjører bygget automatisk før
deploy (sett `SKIP_DEMO_BUILD=1` for å gjenbruke forrige bygg).

Teknologivalg (React + Vite + TypeScript) følger Entur-standarden fra Kompass /
kunnskapsbasen; Linje-designsystemet (`@entur/*`) er bevisst *ikke* brukt siden demoen
skal være merkevare-nøytral.
