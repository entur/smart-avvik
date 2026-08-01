# Varselhistorikk

Intern side på `/historikk`: hvilke avgjørelser Avviksvarsel har tatt om registrerte reiser, hva
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
| `disruptions.varsel_decision_outcomes` | Fasit, satt av `data/queries/label_varsel_decisions.sql` kl. 04:00 UTC. |

Personvern: radene er nøklet på reise*definisjonen* (en digest) pluss et enhetsantall — ingen
`device_id`, ingen `journey_id`, og verken tidsvindu eller ukedagsmønster lagres. Loggen er en
oversikt over hva vi sa om et tog, ikke over hvor noen pendler.

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
