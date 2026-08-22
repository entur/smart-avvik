# Meldinger

Intern side på `/meldinger`: hva ville **dagens prompt** skrevet om avvikene vi allerede har
vurdert?

## Hvorfor den finnes

`/historikk` svarer på «hva sendte vi, og hadde vi rett?». Denne siden svarer på noe annet: «hva
ville vi skrevet nå?». Den plukker et vindu av virkelige varselsykluser, leser det
*skrivegrunnlaget* (`message_input`) som allerede ligger på hver avgjørelsesrad, og kjører det
gjennom systemprompten som gjelder i dag.

Det er sløyfa en promptendring mangler ellers. Uten den er alternativene enten å vente på at ekte
reisende blir varslet under den nye prompten — som tar dager og bare gir de radene trafikken tilfeldig
byr på — eller å teste på oppdiktede eksempler, som ikke ligner. Her er inndataene ekte.

**Ingenting sendes.** Siden spør `llm_message.generation_enabled()` og aldri `delivery_enabled()`.
De to bryterne er ikke det samme: den første sier om en tekst kan *skrives*, den andre bare om en
reisende *mottar* den. prd skriver uten å levere — nettopp derfor er prd-radene de interessante — så
en side som hadde spurt om leveringsbryteren, ville vært tom akkurat der dataene er verdt å se på.

## Vinduet er hele forskjellen

`/historikk` avgrenser på `call_date`, altså hvilken dag toget kjørte, og står som standard på 14
dager. Denne siden avgrenser på `decided_at`, altså **når avgjørelsen ble tatt**, og består av én dag
pluss et klokkeslettintervall — alt i Europe/Oslo.

Tre ting følger av det, og de er alle synlige i grensesnittet:

1. **Begge klokkeslettfeltene bruker native tidskontroller med `step={900}`.** Serveren kvantiserer
   begge ender
   (fra rundes ned, til rundes opp), fordi hver parameter havner i svar-hurtigbufferens nøkkel på et
   åpent, uautentisert endepunkt: to fritt valgte sekundoppløste klokkeslett er i størrelsesorden en
   milliard nøkler per døgn, hver av dem en hurtigbufferrad og en BigQuery-spørring. En kontroll som
   lar deg velge minutter serveren i stillhet flytter på, lyver om seg selv. Siden skriver dessuten
   det *oppløste* vinduet tilbake i feltene, i tilfelle noen skriver inn 14:07 for hånd.
2. **`23:59` i «Til kl.» betyr resten av døgnet.** API-et leser en utelatt `to_time` som neste
   midnatt og avviser en bokstavelig 00:00-slutt. Siden viser derfor den interne 00:00-sentinelen som
   23:59 og sender den som *ingen* parameter.
3. **Standardvinduet er gårsdagen.** Da har nattjobben rukket å sette fasit for langt flere rader enn
   i et vindu fra i dag. Lista åpner dessuten med «Bare der vi varslet» valgt.

## Kortene

Samme kort som `/historikk` — samme utfallsmerker, samme ET- og no-SX-etiketter, samme filtre — med
fire forskjeller som alle er verdt å kunne:

**Formuleringen er skrevet på nytt, ikke sitert.** Den lagrede meldingsteksten serveres bevisst
ikke av dette API-et. Å vise den *sendte* teksten ved siden av en som er regenerert fra en kanskje
annen syklus, ville gjeninnført akkurat den forvekslingen rettelsen 2026-08-21 fjernet fra
`/historikk`, der 41 % av kortene siterte tekst ingen hadde mottatt.

**Hver status har sin egen synlige tilstand.** Ti av dem, og ingen av dem betyr «modellen skrev
ingenting»: `no_input` (ingen varselverdig syklus i vinduet lagret et grunnlag), `unreadable_input`,
`disabled` (ingen backend kunne *spørres* — ikke «dette miljøet sender standardteksten»),
`unavailable`, `error`, `rejected`, `ungrounded`, og de tre rammene `capped`, `rate_limited` og
`deadline`. For `rejected` og `ungrounded` vises **den forkastede teksten**: det er svaret modellen
faktisk skrev, og det er nettopp de to statusene en promptendring lager — et merke uten tekst der
ville skjult det man holder på å iterere på.

**Tallene på kortet gjelder vinduet.** `n_decisions`, første/siste vurdering, «varslet / varslet
ikke», varslingstid og sannsynlighetene er regnet over syklusene *inne i* vinduet, mens utfallet ved
siden av kommer helt og holdent fra nattjobben. Et kort kan derfor med rette si «varslet ikke» og
«traff» samtidig — varselet gikk før vinduet åpnet. De vindusavgrensede feltene er merket med
`vindu`, med forklaringen på hover.

## Filtrene

Fem nedtrekk (**Utfall**, **Operatør**, **Linje**, **Fra**, **Til**) og fire avkryssinger (**Bare med
fasit**, **Bare over terskel**, **Bare der vi varslet**, **Bare med SIRI-SX-melding**). «Bare der vi
varslet» er valgt som standard.

`/meldinger-api/filters` er også API-ets **tillatelsesliste**: en verdi det ikke kjenner gir 400,
ikke tom liste. Derfor henter siden alltid nedtrekksverdiene *først*, rydder bort et valg det nye
vinduet ikke inneholder, og først da spør om rader — ellers ville et vindusbytte blinket en feilmelding
brukeren aldri har forårsaket.

De fire som avgrenser hvilke *avgjørelser* som er med — Operatør, Linje, Fra, Til, i tillegg til dag
og klokkeslett — treffer også `/summary`, slik at tallene beskriver samme utvalg som kortene under
dem. Utfall / «bare med fasit» / «bare der vi varslet» avgrenser på *resultat* og treffer med vilje
bare lista: filtrerer man til treff, blir presisjonen 100 %. API-et nekter dem på `/summary` av
samme grunn.

## Personvern

Arvet ordrett fra `/historikk`, og utvidet med én kolonne. Ingen enkeltrad bærer `journey_digest`,
`from_stop`, `to_stop` — eller `message_input`, som har både start, mål og avgangstid i én blokk.
Grunnlaget leses på serversiden og forlater aldri prosessen; det som forlater den, er teksten som er
*skrevet av* det. `Decision` er `extra="forbid"`, så en framtidig SELECT som legger tilbake en
redigert kolonne feiler høylytt i stedet for å publisere den i stillhet.

## To endepunkter, med vilje

`/decisions` og `/messages` er atskilt. Kortene hentes først og tegnes; formuleringene fylles inn
etterpå. Et Vertex-problem tømmer da formuleringene og lar hvert kort, filter og tall stå — der ett
felles endepunkt ville gitt 503 på hele siden. Listene er stilt opp **posisjon for posisjon** (samme
vindu, samme filtre, samme `ORDER BY`, samme `limit`), og siden *kontrollerer* det mot de fire
identifiserende feltene `/messages` ekkoer, i stedet for å stole blindt på rekkefølgen.

Avspilling er dessuten rammet inn: opptil 1 000 modellkall per forespørsel og time, 5 000 per døgn,
en tidsfrist og en egen hurtigbuffer. En vellykket eller innholdsbestemt formulering ligger i minnet
til neste utrulling (eller til den av kapasitetsgrunner skyves ut); forbigående feil prøves igjen
etter 60 sekunder. Hurtigbufferen sjekkes før kvotene, så en ny lasting bruker ingen modellkall for
kort denne prosessen allerede har skrevet.

## Kjøre lokalt

```sh
# API-et (fra disruptions/api)
uv run uvicorn main:app --reload --port 8000

# Denne appen
npm install
npm run dev        # Vite proxier /meldinger-api til localhost:8000
```

## Bygge

```sh
./build.sh         # -> disruptions/api/meldinger_static/ (gitignorert)
```

I CI bygges den av node-steget i `Dockerfile` i repo-roten. `npm run build` kjører `tsc -b`, så en
TypeScript-feil stopper image-bygget — det finnes ingen egen frontend-jobb, og ingen
frontend-testkjører i dette repoet. Typene er porten.

## Teknologivalg

React + Vite + TypeScript, som de tre andre appene. Bruker Entur Linje (`@entur/*`), i motsetning
til `demo/reisedemo` som med vilje er merkevare-nøytral: denne siden er for oss, ikke for partnere.

`src/api.ts` speiler Pydantic-modellene i `disruptions/api/meldinger.py` for hånd. Det finnes
ingen kodegenerering, så de to kan gli fra hverandre — hold dem i takt.
