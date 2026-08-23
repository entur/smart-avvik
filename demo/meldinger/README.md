# Meldinger

Intern side på `/meldinger`: hva ville **dagens prompt** skrevet om avvikene vi allerede har
vurdert?

## Hvorfor den finnes

`/historikk` svarer på «hva sendte vi, og hadde vi rett?». Denne siden svarer på noe annet: «hva
ville vi skrevet nå?». Den plukker et vindu av virkelige **avganger**, finner varselet som faktisk
gikk ut for hver av dem, leser det *skrivegrunnlaget* (`message_input`) som allerede ligger på
avgjørelsesraden, og kjører det gjennom systemprompten som gjelder i dag.

Det er sløyfa en promptendring mangler ellers. Uten den er alternativene enten å vente på at ekte
reisende blir varslet under den nye prompten — som tar dager og bare gir de radene trafikken tilfeldig
byr på — eller å teste på oppdiktede eksempler, som ikke ligner. Her er inndataene ekte.

**Ingenting sendes.** Siden spør `llm_message.generation_enabled()` og aldri `delivery_enabled()`.
De to bryterne er ikke det samme: den første sier om en tekst kan *skrives*, den andre bare om en
reisende *mottar* den. prd skriver uten å levere — nettopp derfor er prd-radene de interessante — så
en side som hadde spurt om leveringsbryteren, ville vært tom akkurat der dataene er verdt å se på.

## Vinduet er hele forskjellen

`/historikk` avgrenser på `call_date`, altså hvilken dag toget kjørte, og står som standard på 14
dager. Denne siden avgrenser på **`departure_time`** — den planlagte avgangstiden fra påstigningsstoppet
— og består av én dag pluss et klokkeslettintervall, alt i Europe/Oslo. Enheten på siden er en
*melding*, og en melding hører til en avgang.

Fram til 2026-08-22 avgrenset den på `decided_at`, altså når avgjørelsen ble tatt. Den aksen valgte
*sykluser*, ikke meldinger, og kunne derfor ikke svare på «hva ville vi sendt ut for denne avgangen»:
en avgang har ett sendetidspunkt, mens et syklusvindu må plukke ett etter en regel leseren ikke ser.
Den kostet også en hel klasse forbehold — syklusene til ett emne lå på hver sin side av vinduet, så
hvert tall per syklus var vindusavgrenset. På avgangsaksen er et emne **helt inne eller helt ute**,
og alle tallene gjelder hele emnet.

Det som gikk tapt er reelt og verdt å vite: bare et `decided_at`-vindu gjenskaper «hva sa appen kl.
07:15» som ett sammenhengende øyeblikksbilde, og bare det lot leseren sveipe *varslingstid* ved å
flytte vinduet.

Fire ting følger av aksen, og de er alle synlige i grensesnittet:

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
4. **I morgen er en lovlig dag.** Polleren vurderer omtrent 12 timer framover, så kl. 23:00 finnes
   det ekte avgjørelser om en avgang kl. 00:30 i morgen. To dager fram kan ikke inneholde noe og
   avvises, i likhet med en dag lenger tilbake enn 180.

## Kortene

Samme kort som `/historikk` — samme utfallsmerker, samme ET- og no-SX-etiketter, og nesten samme
filtre (SIRI-SX er sidens eget) — med tre forskjeller som alle er verdt å kunne:

**Formuleringen er skrevet på nytt, ikke sitert.** Den lagrede meldingsteksten serveres bevisst
ikke av dette API-et. Å vise den *sendte* teksten ved siden av en som er regenerert fra en kanskje
annen syklus, ville gjeninnført akkurat den forvekslingen rettelsen 2026-08-21 fjernet fra
`/historikk`, der 41 % av kortene siterte tekst ingen hadde mottatt.

**Hver status har sin egen synlige tilstand.** Elleve foruten `ok`, og ingen av dem betyr «modellen
skrev ingenting»: `no_input` (ingen syklus med varsel lagret et grunnlag), `unreadable_input`,
`disabled` (ingen backend kunne *spørres* — ikke «dette miljøet sender standardteksten»),
`unavailable`, `error`, `truncated`, `rejected`, `ungrounded`, og de tre rammene `capped`,
`rate_limited` og `deadline`. For `rejected` og `ungrounded` vises **den forkastede teksten**: det er svaret modellen
faktisk skrev, og det er nettopp de to statusene en promptendring lager — et merke uten tekst der
ville skjult det man holder på å iterere på.

**Fire felter til.** `would_send_at` (syklusen varselet faktisk gikk ut på), `send_state` (hvorfor
emnet har eller ikke har en melding — `sendt`, `sendt_uten_input`, `under_terskel`, `samlet`,
`push_av`, `ikke_sendt`), `other_pushed_on_train` (om en søstermelding om samme tog varslet i stedet)
og `prompt_version` (prompten som skrev radens egen ordlyd).

Og én ting som *ikke* lenger er en forskjell: **tallene på kortet gjelder hele emnet**, akkurat som
på `/historikk`. `n_decisions`, første/siste vurdering, «varslet / varslet ikke», varslingstid og
sannsynlighetene beskriver hele livsløpet, og `alert_lead_min` stemmer med nattjobbens av seg selv.
Fram til 2026-08-22 var de regnet over vinduets *utsnitt* av emnet, så «varslet ikke» kunne med rette
stå ved siden av «traff» — varselet gikk før vinduet åpnet — og feltene bar et `vindu`-merke med
forklaring på hover. Avgangsaksen la de to grunnlagene sammen til ett, og både merkene og
`first_in_window` er borte.

## Filtrene

Seks nedtrekk (**Utfall**, **Operatør**, **Linje**, **Fra**, **Til**, **SIRI-SX**) og fire
avkryssinger (**Bare med fasit**, **Bare over terskel**, **Bare der vi varslet**, **Bare med
SIRI-SX-melding**). «Bare der vi varslet» er valgt som standard.

`/meldinger-api/filters` er også API-ets **tillatelsesliste**: en verdi det ikke kjenner gir 400,
ikke tom liste. Derfor henter siden alltid nedtrekksverdiene *først*, rydder bort et valg det nye
vinduet ikke inneholder, og først da spør om rader — ellers ville et vindusbytte blinket en feilmelding
brukeren aldri har forårsaket.

De fem som avgrenser hvilke *avgjørelser* som er med — Operatør, Linje, Fra, Til og SIRI-SX, i
tillegg til dag og klokkeslett — treffer også `/summary`, slik at tallene beskriver samme utvalg som
kortene under dem. Utfall / «bare med fasit» / «bare der vi varslet» / «bare med SIRI-SX-melding»
avgrenser på *resultat* og treffer med vilje bare lista: filtrerer man til treff, blir presisjonen
100 %. API-et nekter dem på `/summary` av samme grunn.

**SIRI-SX** er sidens eget filter og har ingen tvilling på `/historikk`. Det viser de 30 vanligste
avviksmeldingene i vinduet, **vanligst først** — den rekkefølgen er hele poenget, for en alfabetisk
liste med fritekst svarer ikke på noe. Det er også den eneste **avkortede** ordlista her: et sammendrag
utenfor topp 30 står på kort siden viser, men gir 400 om du filtrerer på det. Prisen for å
tillatelsesliste fritekst på et åpent endepunkt.

To ting til om det:

- **Ett valg kan dekke flere lagrede tekster.** Nedtrekket tilbyr én rad per *svar*, ikke per streng,
  på samme måte som Fra/Til slår sammen flere stoppested-referanser til ett fysisk sted — og serveren
  utvider valget til alle tekstene bak det når den filtrerer.
- **«Ingen» er ikke en melding.** Det er avgangene ingen operatør har publisert noe om
  (`KIX:NoSituation:`), der koden selv skriver et plassholder-sammendrag. Ordlyden på den
  plassholderen har alt endret seg én gang (2026-08-20), så loggen har to tekster som betyr det
  samme; de er slått sammen til én rad. Grupperingen skjer på situasjonsnummerets navnerom og ikke på
  tekstene, nettopp for at en tredje ordlyd skal havne i samme rad av seg selv i stedet for å dukke
  opp som om en operatør hadde skrevet den. Kortene under «Ingen» viser derfor ingen SIRI-SX-linje —
  det er poenget med dem. «Bare med SIRI-SX-melding» skjuler hele gruppa.

## Personvern

Arvet fra `/historikk`, med én kolonne til og én innstramming mindre enn før.

Ingen enkeltrad bærer `journey_digest`, de rå `from_stop`/`to_stop`-referansene eller
`message_input` — det siste har både start, mål og avgangstid i én blokk. Grunnlaget leses på
serversiden og forlater aldri prosessen; det som forlater den, er teksten som er *skrevet av* det.
`Decision` er `extra="forbid"`, så en framtidig SELECT som legger tilbake en redigert kolonne feiler
høylytt i stedet for å publisere den i stillhet.

**Men reisen navngis.** Siden 2026-08-22 slår serveren opp `from_stop`/`to_stop` i
stoppestedsregisteret og serverer `from_name`/`to_name`, som kortet viser. Referansene forlater
fortsatt aldri prosessen, men «en listing parer ikke start med mål» gjelder ikke lenger. Det ble
besluttet i lag med at Fra/Til-filtrene gjorde paret valgbart uansett, og hviler på at
registreringene i prd er testbrukere og ikke publikum. **Tar `/varsel` noen gang imot registreringer
fra publikum, publiserer dette ekte reisevaner** — og svaret er da tilgangsstyring foran
`/meldinger`, ikke en kortere SELECT, siden den samme eksponeringen ligger i den genererte teksten
som ingen projeksjon styrer.

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

Merk at `main:app` starter **polleren**, som bygger prediksjonene om mot BigQuery hvert par minutter.
Det er den dyre delen av appen, og den trengs ikke for å se på en side som bare leser. Skal du bare
jobbe med denne siden, kjør en app som monterer `meldinger.router` alene og injiserer en
BigQuery-klient med `meldinger.configure()`.

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
