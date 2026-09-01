# Rushvarsel — implementasjon (2026-08-29)

> Produktnavnet ble endret fra «Togmeldingen» til «Rushvarsel» 2026-09-01. Endringen er
> bare visuell: rute, API-prefiks, moduler, `disruptions.togvarsel_*`-tabeller, miljøvariabel,
> logghendelser og de to datadiskriminatorene heter fortsatt togvarsel/togmelding.

Dette dokumentet beskriver produktimplementasjonen av anbefalingen i
[02-findings-2026-08-27.md](02-findings-2026-08-27.md) §8, slik §14k reviderte den. Kort:
fire prognoser på hverdager (06:30 og 07:00 for morgenrushet, 12:00 og 14:00 for
ettermiddagsrushet), tunet logistisk regresjon på produktets trøbbelmål (innstilt ELLER ≥ 10 min
forsinket), grønn/gul/rød på studiens faste sannsynlighetskutt, regelbaserte begrunnelser fra de
menneskelesbare featurene, og en deterministisk «kjente avvik»-liste.

## Delene

| del | hvor | hva |
|---|---|---|
| kalender | `data/queries/togvarsel/00_calendar.sql` | `disruptions.togvarsel_calendar` — helligdager + lavsesong, ETT sted; trening, servering og rollover-proben leser alle herfra |
| treningskjede | `data/queries/togvarsel/00–09_*.sql` | batch-kildene → dag-tabellene → `disruptions.togvarsel_training` |
| modelltrening | `data/queries/togvarsel/10_train_models.sql` | `LOGISTIC_REG` med log1p-TRANSFORM og `l2_reg`-grid på holdout; resultat i `disruptions.togvarsel_tuning`, kandidater `togvarsel_m_cand_*` som retrain.sh promoterer til `disruptions.togvarsel_m_{am0630,am0700,pm1200,pm1400}` |
| retrening | `data/queries/togvarsel/retrain.sh` | hele kjeden, månedlig, ≈ $1.5 / ~40 min; promoterer kandidatene først når ALT lyktes, så en feilet retrening aldri etterlater en blandet modellfamilie |
| live features | `disruptions/api/togvarsel_features_today.sql` | serveringstvillingen: samme kolonner fra sanntids-ET/SX → `disruptions.togvarsel_features_today` + `togvarsel_sx_today` |
| serveringsmodul | `disruptions/api/togvarsel.py` | hver poll-tick (veggklokke, uavhengig av SX-ingestion) beregner forfalte prognoser (én features-rebuild + ML.PREDICT per prognose, feil backer av per prognose), `/togmelding-api/today` serverer fra cache |
| side | `demo/togmelding/` (React + Vite + Entur Linje) | `/togmelding`, bygget inn i imaget som de andre frontendene (`disruptions/api/togmelding_static/` er generert og gitignorert fra 2026-09-01, da siden gikk fra én committet HTML-fil til Linje for å matche `/varsel`); skiller «kommer kl. HH:MM» fra «forsinket/utilgjengelig» |
| statuspanel i /varsel | `disruptions/api/togvarsel_notify.py` (`GET /togmelding-api/outlook`) | samme prognose smalnet til én enhet: rushene den faktisk varsles om og som ikke er over ennå, med lenke til `/togmelding`. Push-policyen lest-bare — samme match-tester og samme supersession — men avgrenset av rushets slutt (09:00 / 18:00), ikke av reisevinduet, og leser pollerens stasjonscache uten å spørre BigQuery |
| personverntekster | `demo/togmelding/src/personvern.ts` | duplikat av `demo/avviksvarsel/src/personvern.ts` (appen kan ikke importere den: Docker-steget kopierer bare sin egen katalog), sikret av `disruptions/api/test_personvern_texts.py` — som sammenligner begge modulenes oppløste konstanter og krever at appen faktisk rendrer dem |
| tester | `disruptions/api/test_togvarsel.py` | hermetiske; portene (helg/helligdag/klokkeslett), regelsetningene, feil-/backoff-banene |
| utstedte prognoser | `disruptions.togvarsel_issued` | én rad per (dato, prognose) — MERGE, første skriving vinner; `late` flagger etterberegnede (restart/backoff) som etterprøvingen skal utelate |

## Beslutninger som følger §14k, punkt for punkt

1. **Produktets mål**: alle labels er trouble10 (`am_t10 ≥ 0.10`, rest-av-rush ≥ 0.10,
   `pm_t10 ≥ 0.15`).
2. **Tunet logistisk regresjon, ikke trær**: log1p på tellekolonnene (studiens prefiksregel
   `cat_/catj_/sx_/et_cx/et_n/early_cx`, bakt inn i modellens TRANSFORM så serveringen sender rå
   kolonner) og `l2_reg` valgt på holdout-logloss over de siste 56 dagene (studiens «held-out
   month», utvidet til ~8 uker så rangeringen ikke avgjøres av en håndfull dager), ties brytes
   mot sterkere straff. Refit på alt til slutt.
3. **Featuresett**: 06:30 = F15 + ekstra ET-tilstand + kategorier; 07:00 = AM0700 + kategorier;
   PM-modellene uten kategorier (PM-kategorier feiler placebo, §14b). Ikke vær, ikke rullerende
   persistens.
4. **To morgenprognoser**: 06:30 for hele rushet, 07:00 for resten (den skarpeste, §14e).
5. **Korridornivå**: ikke i v1 — området under ett. Neste naturlige utvidelse; behold da
   korridorens egen tidlig-tilstand og dropp kryss-korridor-features (§14k pkt. 5).
6. **Kvelden før / ettermiddag før kl. 12**: deterministisk tekst, ingen sannsynlighet.
   PM-utsikten om morgenen er to tilstander («normale utsikter» / «noe forhøyet risiko» etter
   rød morgen, med §8-tallene 32 % mot 11 % i teksten).
7. **Sesong**: `skill_note` i payloaden sier eksplisitt at vinterhalvåret er vanskeligere (§14f).
8. **Etterprøving**: hver utstedt prognose lagres i `disruptions.togvarsel_issued`; fasit kan
   regnes fra treningskjedens labels for enhver dato. Nye claims skal fortsatt ha placebo-arm og
   CI (analyseskriptene ligger i `docs/togvarsel/analysis/`).

Nivåkuttene er studiens faste bins på kalibrert sannsynlighet: grønn < 0.10, gul < 0.30,
rød ≥ 0.30 (§14a: grønn ≈ 6 % dårlige dager, rød ≈ 55 %; 07:00-utgaven grønn ≈ 1 %, rød ≈ 61 %).

## Tren/server-parity — hva som er likt og hva som skiller

Serveringsspørringen gjenskaper treningsfeaturene fra sanntidsfeedene med samme vinduer, samme
regexer og samme null-som-null-konvensjon for tellekolonner. Målt 2026-08-29: `prev_eve_t5`
0.4073 live mot 0.4059 i batch-kjeden, `prev_pm_t5` identisk til fjerde desimal.

Kjente, aksepterte skjevheter (dokumentert i SQL-headeren):

- Trening leser døgnets *siste* ET-snapshot; servering leser tilstanden ved beregning. Ratene
  bruker «resolved»-nevnere, så sent innkomne oppdateringer utvider nevneren uten å endre
  betydningen. Beregning skjer alltid PÅ eller ETTER cutoff.
- `sx_amj_*`/`sx_pmj_*` telles mot avgangene ET kjenner akkurat nå; tidlig på dagen kan feeden
  mangle deler av ettermiddagsplanen, så disse kan lese lavt før 12.
- SX-journey-oppslaget i trening går via `togvarsel_journeys` (±1 dag); live bygges
  journey-fotavtrykket av de to siste ET-partisjonene.

## Drift

- **Månedlig**: kjør `data/queries/togvarsel/retrain.sh` (dev). Se på tuning-tabellen den
  printer; holdout-AUC skal ligge rundt §14a/§14e-nivåene, og en vinter-holdout leser lavere
  (§14f). Treningen lander i `togvarsel_m_cand_*`; skriptet promoterer alle fire til de levende
  navnene først når hele kjeden lyktes. Appen plukker opp nye modeller på neste prognose —
  ingen redeploy.
- **Årlig**: utvid `data/queries/togvarsel/00_calendar.sql` (helligdager + lavsesong, ett sted),
  kjør filen og `copy_runtime_to_prd.sh`. Kalenderen dekker nå t.o.m. 17. mai 2027 (`horizon`);
  serveringsspørringen ERRORer høylytt forbi horisonten i stedet for å stille behandle en
  helligdag som hverdag, og retrain.sh nekter å kjøre med under 60 dagers margin.
- **prd**: modellene, `togvarsel_oslo_stations` og `togvarsel_calendar` må finnes i
  prd-prosjektet før siden gir prognoser der (`copy_runtime_to_prd.sh` kopierer alle). Til det
  skjer degraderer siden ærlig: prognosene vises som forsinket/utilgjengelige, «kjente avvik» og
  resten av appen er uberørt.
- **Feil og backoff**: én modells feil dropper aldri de andre prognosene den ticken; en feilet
  prognose prøves igjen etter 15 min og gis opp for dagen etter 8 forsøk (én tydelig loggfeil),
  så en vedvarende feil koster et begrenset antall skanninger — ikke én per poll-tick hele dagen.
  Prognoser beregnet > 10 min etter cutoff (restart-backfill, backoff-retry) flagges `late` i
  både payload og `togvarsel_issued`, og skal utelates fra etterprøving av treffsikkerhet.
- **Kostnad**: ~4 feature-rebuilds + 4 ML.PREDICT per hverdag (to ET-partisjoner + to
  SX-partisjoner per rebuild) — samme størrelsesorden som pollerens eksisterende
  `rail_calls_today`-refresh, og bare en håndfull ganger om dagen; backoffen holder taket
  også når noe er brukket.

## Verifikasjon ved lansering (2026-08-29)

Trening på data t.o.m. 2026-08-28: 343 kvalifiserte hverdager, labels 62/71/58 dårlige dager
(am10/amrest10/pm10) — identisk med studien (§14a: 62/71/57 av 340). Holdout = siste 56 dager
(40 hverdager, sommerhalvår). Valgt `l2_reg` og holdout-AUC per modell:

| modell | l2_reg | holdout-AUC | studiens område |
|---|---|---|---|
| am0630 | 3.0 | 0.81 | 0.74–0.86 (§14a) |
| am0700 | 10.0 | 0.81 | 0.77–0.91 (§14e) |
| pm1200 | 3.0 | 0.75 | — |
| pm1400 | 10.0 | 0.75 | 0.63–0.73 (§14h) |

Sterke straffer vant overalt, som §14j forutsa. Full grid i `disruptions.togvarsel_tuning`.

Røyktest samme kveld mot lørdag 2026-08-29 live (en ren pipelinetest — helgeporten hadde stoppet
en ekte utstedelse): featureraden stemte med batch-kjeden (prev_eve_t5 0.4073 mot 0.4059), og de
fire modellene ga p = 0.107 / 0.067 / 0.129 / 0.074 (gul/grønn/gul/grønn) på en dag der
fredagskvelden var dårlig (eve_t5 0.41), morgenen holdt seg rolig, og realisert am_t10 ble 0.0 —
riktige nivåer, koherent historie. Treningskjeden + treningen kostet ≈ $0.4; hele
implementasjonsøkten inkludert røyktester ≈ $0.7. Etter lanseringsgjennomgangen (multi-agent
kodegjennomgang + kryssmodell-motpart) ble kalenderjoinen, backoffen og MERGE-skrivingen
verifisert på nytt mot dev: treningstabellen identisk (343/62/71/58), live-raden identisk
(prev_eve_t5 0.4073), modellkopi-promoteringen testet.
