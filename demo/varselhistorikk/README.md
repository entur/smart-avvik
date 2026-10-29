# Varselhistorikk

Intern side på `/historikk`: hvilke avgjørelser Smarte reisevarsler har tatt om registrerte reiser, hva
som ble sendt til folk, og hvordan det faktisk gikk.

## Hvorfor den finnes

Fram til nå fantes det ingen fasit. Meldingsteksten lå bare i Cloud Logging, `varsel._messages` er
bevisst bare i minnet (den tømmes ved redeploy), og ingenting registrerte hva som *skjedde* med
toget vi varslet om. Da er spørsmålet som betyr noe for å tune systemet umulig å svare på: når vi
varslet, hadde vi rett?

Loggen inneholder **alle avgjørelser**, ikke bare de vi varslet om. Det er hele poenget: en reise
vi valgte å tie om setter ingen spor, så uten dem kan vi bare måle presisjon — aldri hvor mye vi
gikk glipp av.

## To ting siden må aldri gjøre

1. **Ukjent utfall er ikke «gikk fint».** Fant vi ikke igjen avgangen i sanntidsdata, er utfallet
   ukjent. `last_recorded` er ~10–17 % tynnere enn anshar på tog, så noen avganger blir aldri
   avklart. Teller vi dem som «riktig stille», lager vi presisjon av manglende data.
2. **«Venter på fasit» er ikke det samme som «ukjent».** Det første ordner seg over natta, det
   andre aldri. Slår vi dem sammen, ser et ferskt miljø ut som et ødelagt et.

## Datakilder

| | |
|---|---|
| `disruptions.varsel_decisions` | Append-only. Skrives av poller-syklusen (`disruptions/api/decisions.py`). |
| `disruptions.varsel_decision_groups` | View: én rad per avgjørelse, delt med nattjobben. |
| `disruptions.varsel_decision_outcomes` | Fasit, satt av `data/queries/label_varsel_decisions.sql` hver morgen kl. 05.30 norsk tid. |

Personvern: radene er nøklet på reise*definisjonen* (en digest) pluss et enhetsantall — ingen
`device_id`, ingen `journey_id`, og verken tidsvindu eller ukedagsmønster lagres. Loggen er en
oversikt over hva vi sa om et tog, ikke over hvor noen pendler.

## Filtrene

Fem nedtrekk, alle med «Alle» som standard: **Utfall**, **Operatør**, **Linje**, **Fra** og **Til**.
De fire siste fylles fra `GET /historikk-api/filters` over hele tidsvinduet — aldri fra kortene på
skjermen, som er kappet på 200 og sortert på avgang: en linje som bare kjørte tidlig i vinduet ville
da falle ut av sitt eget filter, og et valgt filter ville krympe lista det kom fra til seg selv.

Linjeverdiene er *refs* (`VYG:Line:R11`) — det er dem loggen lagrer og API-et sammenligner mot.

**Fra/Til er ett fysisk sted per rad, ikke én ref.** En stasjons togside og bussside er to ulike
NSR-refs under samme `parentRef`, og en registrering havner på den ene eller den andre. De slås
derfor sammen til én rad — samme regel som `main.refresh_varsel_stops` bruker for velgeren: samme
forelder *og* samme navn (forelder alene ville slått sammen Oslo S og Jernbanetorget; en ref uten
navn slås aldri sammen). Raden peker på den travleste av refsene, og **filtrerer på alle sammen**.
Det siste er poenget: matcher man bare den ene, forsvinner resten i stillhet — 659 av Asker stasjons
842 avgjørelser lå bak den refen raden *ikke* var oppkalt etter (målt i dev 2026-08-21).

Navn og forelder hentes rett fra Nasjonalt stoppestedsregister (`historikk_stop_places.sql`), ikke
fra stoppestedslista bak `/varsel-api/stops`: den lista inneholder bare dagens rutetabell og har
allerede kastet den refen som tapte sammenslåingen. Uten registeret vises refs, og filtrene virker
like fullt.

`/filters` er også API-ets tillatelsesliste: en verdi som ikke finnes i vinduet gir 400, ikke tom
liste. Derfor rydder siden i valgene sine hver gang vinduet endres — velger du en linje i 30-dagers­
vinduet og går ned til 7 dager, nullstilles valget i stedet for at du får en feilmelding.

**Fra/Til er reisens egne endepunkter**, ikke avvikets geografi: de avgrenser til avgjørelser tatt
om reiser noen har registrert mellom de to stedene. Det er en bevisst innsnevring av personvern­
løftet over (2026-08-21): paret kan velges og avgangene leses av. Det som *ikke* er endret, er at
ingen enkeltrad bærer `from_stop`/`to_stop`/`journey_digest` — et kort navngir fortsatt et tog, ikke
en reise. Vil vi ha sterkere garanti enn det, er svaret tilgangsstyring på `/historikk`, ikke en
kortere SELECT — og den finnes siden 2026-09-10: siden og `/historikk-api` ligger bak Entur-SSO
(`sso.py`, Auth0s partner-tenant med Entra-innlogging, bare organisasjon Entur) i alle miljøer.
Redaksjonen over står likevel som før; porten er én miljøvariabel fra å være av.

Utfall/«bare med fasit»/«bare der vi varslet» avgrenser derimot på *resultat* og treffer med vilje
bare lista, aldri `/summary`: filtrerer man til treff, blir presisjonen 100 %. De fire første
avgrenser hvilke *avgjørelser* som er med, og treffer derfor begge — tallene beskriver samme utvalg
som kortene under dem.

## Kjøre lokalt

```sh
# API-et (fra disruptions/api)
uv run uvicorn main:app --reload --port 8000

# Denne appen
npm install
npm run dev        # Vite proxier /historikk-api til localhost:8000
```

## Bygge

```sh
./build.sh         # -> disruptions/api/historikk_static/ (gitignorert)
```

I CI bygges den av node-steget i `Dockerfile` i repo-roten. `npm run build` kjører `tsc -b`, så en
TypeScript-feil stopper image-bygget — det finnes ingen egen frontend-jobb.

## Teknologivalg

React + Vite + TypeScript, som de to andre appene. Bruker Entur Linje (`@entur/*`), i motsetning
til `demo/reisedemo` som med vilje er merkevare-nøytral: denne siden er for oss, ikke for partnere.
