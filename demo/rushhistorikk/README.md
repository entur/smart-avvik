# Rushhistorikk (`/rushhistorikk`)

Traff rushvarslene? Én rad per dag: de fire prognosene (06:30, 07:00, 12:00, 14:00), hva vi faktisk
meldte for hver av dem, og hva de to rushene endte som. React + Vite + [Entur Linje], servert av
disruptions-API-et på `/rushhistorikk`, med data fra `GET /rushhistorikk-api/days`
(`disruptions/api/rushhistorikk.py`).

Dette er for **Rushvarsel** hva `/historikk` er for `/varsel`. Siden er søsteren til `/historikk` og
skal leses som det: samme layout-primitiver, samme chip-palett, samme kortspråk — bare med prefikset
`rh-` i stedet for `vh-`.

## Hvorfor den finnes

Rushvarsel sender ut fire prognoser per hverdag og pusher noen av dem, og ingenting målte om noe av
det traff. Nivå-mot-fasit-halvdelen fantes fra før (§14a i `docs/togvarsel/02-findings-2026-08-27.md`:
grønn prognose ≈ 6 % dårlige dager, gul ≈ 21 %, rød ≈ 55 %) — det som manglet var **varsel-halvdelen**:
om noen faktisk fikk beskjed. Den fantes bare som en `{level, pushed}`-markør i `/varsel`-tilstanden,
og `varsel.store_togvarsel_rounds` erstatter hele kartet med **én dato**, så gårsdagens forsvinner
ved første skriving etter midnatt.

Poenget med siden er å kunne argumentere for en strammere terskel. Rushvarsel sin egen dokumentasjon
sier at gult-eller-rødt slår inn på ca. **56 %** av hverdagsmorgenene, og de dokumenterte
oppfølgingene (bare rødt, nivå per bruker) er terskelendringer ingen kan begrunne uten en treffrate.

## Tre ting siden aldri må gjøre

1. **Slå sammen de tre måtene å ikke vite.** `venter` (fasiten prøves fortsatt), `ukjent` (den kan
   ikke fastslås mer, eller dagen strøk på kvalifiseringstesten) og `ikke vurdert` (ingen runde ble
   registrert, og den kunne heller ikke rekonstrueres). Å slå to av dem sammen lager ferdighet av
   manglende data, i den ene eller andre retningen.
2. **Vise et foreløpig utfall som avklart.** Utfallet fra samme dag har fire kjente skjevheter
   (se toppen av `disruptions/api/togvarsel_outcomes_provisional.sql`) og kan endre seg over natten.
   Det vises på dagraden, merket «foreløpig», og holdes utenfor hver eneste rate.
3. **Publisere abonnent-tallene.** `togvarsel_rounds` har `matched_journeys`, `matched_devices`,
   `n_sent`, `n_error` og `n_gone` — dagvise tall over en abonnentmasse på en håndfull.
   `rushhistorikk_days.sql` nevner dem ikke, og det er hele vernet: en spørring som ikke kan navngi
   en kolonne kan ikke lekke den, uansett hvordan laget over drifter.

## Datakilder

Tre poster møtes, og to av dem ble laget for denne siden:

| Tabell | Hva den svarer på |
|---|---|
| `disruptions.togvarsel_issued` | hva vi varslet, og om prognosen var sen |
| `disruptions.togvarsel_rounds` | hva vi meldte — og hvorfor ikke, når vi ikke gjorde det |
| `disruptions.togvarsel_outcomes` | hva rushet faktisk ble, per vindu |

Ingen av de to nye ligger i `copy_runtime_to_prd.sh`: hvert miljø avgjør sine egne prognoser, og
`bq cp -f` overskriver.

## Verdiktene

Verdiktet vurderer **det som ble meldt**, ikke det prognosen sa. En oppfølging som bekrefter
baselinen sender ingenting, men den reisende har fortsatt varselet fra baselinen — og det er *det*
nivået som vurderes. Uten den regelen ville 07:00 og 14:00 lest `bom` hver eneste varslet dag.

| Verdikt | Betyr |
|---|---|
| Traff | riktig klasse ble meldt — riktig nivå varslet, eller ingenting varslet og rushet ble grønt |
| Traff nesten | rødt varsel og gult rush, eller omvendt |
| Falsk alarm | det som ble meldt stemte ikke |
| Bom | et varsel som burde gått ut, gikk ikke |
| Venter / Ukjent / Ikke vurdert | de tre måtene å ikke vite (se over) |

«Traff» tar med tausheten (siden 2026-09-02): grønn prognose på et grønt rush er et treff, akkurat
som gult varsel på et gult rush. Før det var det et eget verdikt, «riktig stille», som ikke telte i
Presisjon — og siden 82 % av dagene er grønne leste tallet 0 % en god måned, rett fra definisjonen.

Logikken ligger i `disruptions/api/rushhistorikk_verdicts.py` — rene funksjoner, uten I/O, med 37
sjekker og en enumerasjon over alle nåbare kombinasjoner. Den finnes bare ett sted nettopp fordi
tabellen har fjorten grener; skrevet en gang til i SQL blir det to definisjoner av «traff».

## Rekonstruerte runder

Runde-loggen (`togvarsel_rounds`) begynte å skrives 2. september 2026, mens prognoseloggen
(`togvarsel_issued`) går lenger tilbake — og siden skal først og fremst bedømme **modellen**. For en
utstedelse uten registrert runde kjører derfor API-et (`rushhistorikk.reconstruct_rounds`) selve
push-regelverket, `togvarsel_notify.decide_issue`, på prognosen slik den forelå, ved prognosens eget
`computed_at`, grunnvarsel før oppfølging, med dagens registrerte runder som markørtilstand.
Resultatet vurderes som en vanlig runde, men er merket «rekonstruert» på dagraden, telt for seg under
tabellen («Rekonstruert», og «Mangler runde» for dem på dager loggen allerede dekket), og aldri regnet
som et sendt varsel: «Varsler sendt» teller bare registrerte push, og viser de rekonstruerte i
parentes.

En registrert runde overstyres aldri. Det en rekonstruksjon ikke kan se — en runde porten lukket før
den ble vurdert, hvor mange enheter som ble truffet — er akkurat det modellbedømmelsen ikke trenger.
Fasit for eldre dager fylles inn av den autoritative passeringen, som rekker 60 dager tilbake for
dager uten utfallsrad (`togvarsel_outcomes._BACKFILL_DAYS`).

## Tallene i «Slik har varslene truffet»

Tre populasjoner, fordi tallene ikke er over samme rader:

- **`scored`** — runde registrert, dag kvalifisert, fasit avklart, prognose ikke sen, runde innenfor
  frist. Presisjon og treffmatrisen.
- **`delivered`** — som over, men **med** sene og for-sent-vurderte. Dekning, fordi et rush ingen ble
  varslet om er et dekningstap uansett hva som forurenset prognosen.
- **`recorded`** — alle utstedelser. Det er grunnlaget eksklusjonstellingene deler opp, og det
  «Varsler sendt» teller push over: en push er sendt uansett hva dagen ble, uavklarte dager
  medregnet, friskmeldinger medregnet — én per runde.

To fallgruver som er lette å snu: en **friskmelding** er ikke et varsel, så den holdes utenfor
telleren i Dekning — en gal friskmelding skal ikke få kreditt for å ha varslet om et rødt rush. Og
**Dekning på totalraden teller rush, ikke utstedelser**, siden en bekreftende oppfølging bare er
dekket via baselinen sin.

`Varslingstid` måles fra hvert vindus egen start (06:30 / 07:00 / 15:00), positivt når pushen kom
før rushet. Morgenutstedelsene lander derfor ~1 minutt *inne* i sitt eget rush. Det er et funn om
produktet, ikke et tall som skal pyntes på.

Tabellen hadde fem tall til 2026-09-02: «Presisjon (samlet)», «Presisjon (varslet)» og
«Avgjørelser» ble til «Presisjon» (traff over alt avklart) og «Varsler sendt» (antall push). Det
første fordi «samlet» leste 0 % en god måned, det siste fordi fire per hverdag uansett hva som ble
sendt ikke sier noe om varselvolum — som er det tretthetsspørsmålet handler om.

## Kjøre lokalt

Ikke kjør det ekte API-et for å se på siden: det bygger en BigQuery-klient ved oppstart og pollerens
sykel skanner ET-partisjoner på flere GB uten av-knapp. Stub `:8000` med `http.server` og kanned
JSON på `/rushhistorikk-api/config` og `/rushhistorikk-api/days`, og kjør:

```sh
npm install
npm run dev      # Vite proxyer /rushhistorikk-api til :8000; åpne /rushhistorikk/
```

`base` i `vite.config.ts` er `/rushhistorikk/`, så URL-en lokalt har med prefikset.

## Bygge

```sh
./build.sh       # npm install + npm run build -> disruptions/api/rushhistorikk_static/
```

Utdata er gitignorert; bildet bygger den selv i node-steget i `Dockerfile` i rota. `npm run build`
kjører `tsc -b` først, så en typefeil feiler image-bygget — det finnes ingen egen frontend-jobb i CI.

## Tilgang

Siden er bak Entur-SSO (`sso.py`, Auth0s partner-tenant med Entra-innlogging, kun for
organisasjon Entur) på alle tre hostene siden 2026-09-10, som søsknene sine: `sso.PROTECTED_PREFIXES`
har både `/rushhistorikk` og `/rushhistorikk-api`, og hver miljøfil setter `SSO_REQUIRED: "true"` med
sin egen Auth0-klient. Fram til da var siden åpent lesbar (`"false"` i `helm/kix-avvik/values.yaml`,
ikke overstyrt). Å skru porten av igjen er én verdi per miljø, ikke en kodeendring.

[Entur Linje]: https://linje.entur.no/
