# Reisedemo — prediksjoner mot fasit

Demoside for `/disruptions`-API-et: viser hvor godt modellene predikerer forsinkelser og
innstillinger for en passasjerreise. Velg fra-/til-stasjon og tidsrom (eller et
ferdigplukket eksempelscenario), og siden viser alle matchende reiser med

- prediksjonene slik de sto **da SX-meldingen kom**, **60 min før** og **30 min før**
  toget (hver med kalibrering for sin spørrehorisont),
- **faktisk utfall** (innstilt / forsinkelse i minutter), og
- en ✓/✕-indikator for om varselsbeslutningen (terskel 0,65 på
  P(innstilt eller ≥ 10 min forsinket)) traff.

Øverst i resultatlisten oppsummerer **«Slik ville varslene truffet»**
(`src/HitSummary.tsx`) hele søket: presisjon og dekning målt på siste prediksjon før
avgang, med en utslåbar oppdeling per spørretidspunkt. Motstykket til «Slik har varslene
truffet» på `/historikk`, men i konjunktiv — ingen av disse varslene ble sendt, terskelen
er spilt av på nytt over holdoutet. Alt telles **per avgang på påstigningsstoppet**, som
er beslutningen et varsel faktisk er; ankomstsiden på kortene inngår ikke.

Avgang vises til venstre, ankomst til høyre. Bevisst nøytral formgivning — demoen vises
for partnere med egne designspråk.

## Data

To lag, samme tabeller (`disruptions.demo_predictions`, `demo_calls`, `demo_stops`,
`demo_highlights`) — alle tall er utenfor treningsdataene:

- **Holdout** (NSB-tog, 1. apr – 20. jun 2026): bygges av
  `data/queries/create_demo_predictions.sql`, som gjenbruker `leadtime_scored` +
  per-horisont-platt-lagene og speiler serveringsmatematikken i
  `disruptions/api/predict_calls.sql`. Kjør på nytt etter
  modell-/kalibreringsretrening (da må også recent-laget kjøres på nytt).
- **Ferske dager** (siste ~25 dager, t.o.m. i dag): `data/queries/refresh_demo_recent.sh`
  (kjører `extend_demo_recent.sql`, ~$2) henter realiserte avganger rett fra SIRI-ET/SX,
  regner ut featurene på nytt forankret ved spørretidspunktene og scorer alle ni hodene.
  Kjør før en partnerdemo; kjøringer inntil ~3 uker fra hverandre holder vinduet tett.
  API-et plukker opp det nye vinduet innen ~30 min (metadata-cache) uten redeploy.

## Kjøre lokalt

```sh
# backend (serverer /demo-api mot BigQuery under din ADC)
cd disruptions/api && uv run uvicorn main:app --port 8000

# frontend med hot reload (proxyer /demo-api til :8000)
cd demo/reisedemo && npm install && npm run dev
```

## Bygge og deploye

`./build.sh` bygger til `disruptions/api/demo_static/` (generert katalog, ikke i git);
API-et serverer den på `/demo`. For deploy bygges front-endene automatisk inn i imaget
(rot-`Dockerfile`ens node-steg via `.github/workflows/ci.yml`), så `build.sh` trengs bare
for lokal utvikling.

Teknologivalg (React + Vite + TypeScript) følger Entur-standarden fra Kompass /
kunnskapsbasen; Linje-designsystemet (`@entur/*`) er bevisst *ikke* brukt siden demoen
skal være merkevare-nøytral.
