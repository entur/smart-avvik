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
   noensinne vurdert — porten var lukket, eller dagen er eldre enn loggen). Å slå to av dem sammen
   lager ferdighet av manglende data, i den ene eller andre retningen.
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
| Traff | det som ble meldt stemte |
| Traff nesten | rødt varsel og gult rush, eller omvendt |
| Falsk alarm | det som ble meldt stemte ikke |
| Bom | et varsel som burde gått ut, gikk ikke |
| Riktig stille | ingenting meldt, ingenting påkrevd |
| Venter / Ukjent / Ikke vurdert | de tre måtene å ikke vite (se over) |

Logikken ligger i `disruptions/api/rushvarsel_verdicts.py` — rene funksjoner, uten I/O, med 38
sjekker og en enumerasjon over alle nåbare kombinasjoner. Den finnes bare ett sted nettopp fordi
tabellen har fjorten grener; skrevet en gang til i SQL blir det to definisjoner av «traff».

## Tallene i «Slik har varslene truffet»

Tre populasjoner, fordi tallene ikke er over samme rader:

- **`scored`** — runde registrert, dag kvalifisert, fasit avklart, prognose ikke sen, runde innenfor
  frist. Presisjon og treffmatrisen.
- **`delivered`** — som over, men **med** sene og for-sent-vurderte. Dekning, fordi et rush ingen ble
  varslet om er et dekningstap uansett hva som forurenset prognosen.
- **`recorded`** — alle utstedelser. Avgjørelser.

To fallgruver som er lette å snu: en **friskmelding** er ikke et varsel, så den holdes utenfor
Presisjon (varslet) og utenfor telleren i Dekning — ellers leser tjue riktige friskmeldinger og tjue
gale røde varsler som 50 %. Og **Dekning på totalraden teller rush, ikke utstedelser**, siden en
bekreftende oppfølging bare er dekket via baselinen sin.

`Varslingstid` måles fra hvert vindus egen start (06:30 / 07:00 / 15:00), positivt når pushen kom
før rushet. Morgenutstedelsene lander derfor ~1 minutt *inne* i sitt eget rush. Det er et funn om
produktet, ikke et tall som skal pyntes på.

> **`Presisjon (samlet)` leser 0 % en god måned**, og det følger av definisjonen: «andel traff» over
> alle avklarte verdikt, der et korrekt grønt rush aldri gir `traff` — og grønt er 82 % av dagene.
> Definisjonen er beholdt som spesifisert og den degenererte lesningen er pinnet i en test; se
> Open Questions i planen for de tre veiene ut.

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

Siden er **åpent lesbar** på alle tre hostene i dag, som søsknene sine. `sso.PROTECTED_PREFIXES` har
både `/rushhistorikk` og `/rushhistorikk-api`, men `SSO_REQUIRED` er `"false"` i
`helm/kix-avvik/values.yaml`, ikke overstyrt i noe miljøfil, og ingen miljø har OAuth-klienten — så
porten kortslutter før prefiksene sjekkes. Registreringen er det som gjør at å skru den på er én
verdi, ikke en kodeendring.

[Entur Linje]: https://linje.entur.no/
