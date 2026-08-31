# PRD: Personverninformasjon, samtykke og sletting i Avviksvarsel

**Omfang:** A2, A3, A4, A7 fra handlingslisten
**Dato:** 30. august 2026
**Grunnlag:** verifisert mot kjørende `kix-avvik.entur.io` 30.08.2026 — ikke mot dokumentasjon
**Ute av omfang:** A1 (tilgangskontroll), A5 (`/eval`), A6 (`device_id`), A8–A12

---

## 1. Verifisert utgangspunkt

Alt i denne tabellen er observert direkte i kjørende tjeneste. Ingenting er hentet fra `CLAUDE.md`.

| Observasjon | Hvordan verifisert |
|---|---|
| Ingen personverninformasjon på `/varsel` | Full `innerText` av siden lest — ingen personverntekst, ingen lenke, ingen bunntekst |
| Ingen personvernside finnes | `GET /varsel/personvern`, `/personvern`, `/varsel-api/privacy` → alle **404** |
| «Legg til reise» har ingen samtykke- eller informasjonstekst | Modalen åpnet, full `innerText` lest: Transportmiddel · Fra · Til · Fra/Til klokkeslett · Ukedager · Avbryt · Lagre reise |
| Ingen eksperimentforbehold noe sted | Samme `innerText` |
| `device_id` genereres med `crypto.randomUUID()` | Funnet i utsendt JS-bundle |
| `device_id` lagres som `avviksvarsel.device_id` i `localStorage` | Lest ut: `7a46b448-90e9-4af9-91d5-f898b951460e` |
| **`localStorage` tømmes aldri** | Søk etter `removeItem` i hele bundlen: **null treff** |
| «Skru av» avmelder push **og** sletter enhetsposten på server | Handler i bundlen: `await _a(); await aa(t)` → `subscription.unsubscribe()` + `DELETE /varsel-api/devices/{device_id}` |
| Enkeltreise kan slettes | `DELETE /varsel-api/journeys/{id}?device_id=…` |
| Ingen samlet «slett alt» | Ingen slik handler i bundlen |
| API-et har ingen autentisering | `GET /varsel-api/journeys?device_id=<tilfeldig UUID>` → **200** `{"journeys":[]}` |
| `device_id` sendes i URL-en | Nettverkslogg: `GET /varsel-api/journeys?device_id=7a46b448-…` |
| `device_id` sendes til en loggingsendepunkt | `POST /varsel-api/client-log` med `{context, message, stack, device_id, url}` |
| **`?brand=none` viser ordet «Entur» ingen steder** | Rendret side: `document.title` = `Avviksvarsel`, `/entur/i.test(document.body.innerText)` = **false** |
| Den avmerkevarede varianten installeres som egen app | `manifest-none.webmanifest`: `name: "Avviksvarsel"`, `display: "standalone"`, `start_url: "/varsel/?brand=none"` |
| Registrering fungerer likt i begge varianter | `POST /varsel-api/devices` tar `{device_id, subscription, brand}` — merkevaren er en parameter, ikke en sperre |

**Full API-flate** (uttrekk fra bundlen, ikke fra dokumentasjon):
`GET config` · `GET stops` · `GET journeys?device_id` · `POST journeys` · `DELETE journeys/{id}?device_id` · `POST devices` · `DELETE devices/{device_id}` · `POST test` · `POST feedback` · `POST client-log`

---

## 2. A2 — Personverninformasjon på flaten

### Argument

Informasjon etter GDPR art. 13 nr. 1 skal gis **på det tidspunktet personopplysningene innhentes**. Det er verifisert at ingen slik informasjon finnes noe sted i `/varsel`, og at ingen personvernside eksisterer.

Enturs erklæring på `om.entur.no/personvern` dekker ikke dette: den omtaler ikke eksperimenter, og informasjonsplikten skal uansett oppfylles der opplysningene innhentes — ikke på et annet domene brukeren ikke besøker.

Art. 12 nr. 1 krever «kortfattet, åpen, forståelig og lett tilgjengelig form, på et klart og enkelt språk». Lagdelt informasjon er tillatt, men **første lag må bære det vesentlige** — en ren lenke uten innhold oppfyller ikke kravet.

### Løsning

Fire korte tekster på flaten, og én side de peker til. Én React-komponent, gjenbrukbar på `/togmelding`.

### T1 — Eksperimentforbehold

Plassering: øverst på siden, over introsetningen. Nøytral infoboks.

> **Dette er et eksperiment**
> Avviksvarsel er et eksperiment fra Enturs KI-lab. Vi tester om varsler før avviket rammer er til nytte. Tjenesten kan endres eller avsluttes på kort varsel, og varslene er anslag som ikke alltid treffer — ikke planlegg reisen din på dem alene.

**Rasjonale:** art. 5 nr. 1 bokstav a (rettferdighet og åpenhet). Slik siden står i dag leser den som et ferdig Entur-produkt. En bruker som legger om morgenrutinen etter varslene, og som ikke er fortalt at de er anslag, er ikke rettferdig behandlet.

### T2 — Personvernlinje

Plassering: rett under introsetningen, som brødtekst.

> Entur AS lagrer reisene du registrerer, slik at vi kan varsle deg. [Slik behandler vi opplysningene](#)

### T3 — Registreringstekst

Plassering: i «Legg til reise», over knappene, i full bredde. **Erstatter ingenting — det står ingen tekst der i dag.**

> Vi lagrer holdeplassene, ukedagene og tidsrommet du velger her. Vi trenger dem for å vite hvilke avvik som gjelder deg. Du kan slette reisen når som helst. [Slik behandler vi opplysningene](#)

### T4 — Varselkort

Plassering: i kortet med «Slå på varsler» / «Skru av», under statuslinjen.

> For å sende varsler lagrer vi en teknisk nøkkel til denne nettleseren. Den brukes bare til å varsle om reisene dine, og slettes når du trykker «Skru av». Innholdet i varselet er kryptert, slik at tjenesten som frakter det ikke kan lese det.

**Rasjonale:** art. 13 nr. 1 bokstav c og e. Push-abonnementet er en egen personopplysning — en unik, vedvarende identifikator for én nettleserinstallasjon — og brukeren blir ikke fortalt at den lagres.

### T5 — Personvernsiden

Ny rute `/varsel/personvern` (bekreftet ledig — 404 i dag). Verdier i `[hakeparentes]` må besluttes, se punkt 7.

> # Personvern i Avviksvarsel
>
> Avviksvarsel er et eksperiment fra Enturs KI-lab. Denne siden forklarer hvilke opplysninger vi behandler når du bruker tjenesten.
>
> **Behandlingsansvarlig**
> Entur AS, Postboks 1800, 0048 Oslo. Personvernombudet nås på personvern@entur.org.
>
> **Hva vi lagrer**
> Reisene du registrerer: holdeplass du reiser fra, holdeplass du reiser til, transportmiddel, tidsrom og ukedager. Slår du på varsler, lagrer vi i tillegg en teknisk nøkkel til nettleseren din og et varselabonnement. Vi lagrer ikke navn, e-postadresse eller telefonnummer, og du trenger ingen bruker for å bruke tjenesten.
>
> **Hvorfor**
> For å varsle deg når et avvik kan ramme en reise du har registrert, og for å forbedre varslene.
>
> **Rettslig grunnlag**
> Registrering av reiser og varsler behandles på grunnlag av ditt samtykke, jf. personvernforordningen art. 6 nr. 1 bokstav a. Du gir samtykket ved å registrere en reise, og trekker det tilbake ved å slette den. Forbedring av varslene skjer på grunnlag av vår berettigede interesse i å vite om tjenesten er til nytte, jf. art. 6 nr. 1 bokstav f.
>
> **Hvem som kan se opplysningene**
> Ansatte i Entur som arbeider med tjenesten kan se hvilke reiser som er registrert, for å vurdere om varslene treffer. [Google Cloud drifter tjenesten på Enturs vegne, i EU.] [Varselteksten skrives av en språkmodell hos Google i EU, som får vite hvilken strekning og avgang varselet gjelder.] Tjenesten som leverer varselet til telefonen din ser ikke innholdet — det er kryptert.
>
> **Hvor lenge**
> Reiser du har registrert lagres til du sletter dem, og fjernes automatisk etter [90] dager uten aktivitet. Varselabonnementet slettes når du skrur av varsler. Loggen over varsler vi har vurdert å sende lagres i [X] dager.
>
> **Rettighetene dine**
> Du kan slette reisene dine og varselabonnementet direkte i appen. Du har også rett til innsyn, retting, begrensning, protest og dataportabilitet, jf. art. 15 til 21. Fordi tjenesten ikke vet hvem du er, må du bruke enheten du registrerte reisene fra for at vi skal kunne finne dem.
>
> **Klage**
> Mener du at vi behandler opplysningene dine i strid med regelverket, kan du klage til Datatilsynet.
>
> **Overføring ut av EØS**
> [Behandlingen skjer i EU/EØS.]

### Oppgaver

| Oppgave | Rolle |
|---|---|
| Bygg `PersonvernNotis`-komponent for T1–T4 | Frontend |
| Ny rute `/varsel/personvern` med T5 | Frontend |
| Beslutt verdiene i hakeparentes | PM + utvikler |
| Gjenbruk på `/togmelding` | Frontend |

---

## 3. A3 — Samtykke ved registrering

### Argument

Registrering av en reise oppretter et pendlemønster: strekning, ukedager og tidsvindu. Det er verifisert at dette skjer uten at brukeren får noen informasjon, og uten at noen tillatelse etterspørres. Nettleserens varselstillatelse er en teknisk tillatelse for push, ikke et samtykke etter personvernforordningen, og den utløses ikke ved registrering.

### Løsning

**Ingen avkrysningsboks.** Art. 4 nr. 11 definerer samtykke som «en frivillig, spesifikk, informert og utvetydig viljesytring … i form av en erklæring **eller en tydelig bekreftelse**». Å fylle ut fra, til, klokkeslett og ukedager og trykke «Lagre reise» **er** en tydelig bekreftelse.

Det som mangler er ikke bekreftelsen. Det er informasjonen som gjør den *informert*. **T3 er hele tiltaket.**

### Rasjonale

Samtykke er riktig grunnlag her framfor art. 6 nr. 1 bokstav b (avtale), fordi tjenesten er gratis og eksperimentell uten avtalevilkår, og framfor bokstav f (berettiget interesse), fordi en bruker ikke har rimelig forventning om at et pendlemønster lagres uten at det er sagt.

Formuleringer av typen «ved å trykke lagre samtykker du til …» bør unngås: de **påstår** samtykke framfor å informere, og en henvisning uten innhold oppfyller ikke informasjonskravet i art. 4 nr. 11.

### Oppgaver

| Oppgave | Rolle |
|---|---|
| Legg T3 inn i «Legg til reise» over knapperaden | Frontend |
| Loggfør at samtykketeksten som ble vist er versjonert | Frontend + backend |

**Merknad om dokumentasjon:** art. 7 nr. 1 legger bevisbyrden for samtykke på behandlingsansvarlig. Så lenge tjenesten bevisst ikke vet hvem brukeren er, kan samtykket ikke logges per person. Det som *kan* dokumenteres er hvilken tekstversjon som var i produksjon når. Versjonsnummer i koden er tilstrekkelig, og bør nevnes i personvernvurderingen.

---

## 4. A4 — Sletting

### Argument

Verifisert: `DELETE /varsel-api/devices/{device_id}` og `DELETE /varsel-api/journeys/{id}` finnes begge, og «Skru av» kaller den første. **Backend-arbeidet er i praksis gjort.**

Tre hull, alle verifisert:

1. `localStorage` tømmes aldri — søk etter `removeItem` i hele bundlen gir null treff. `avviksvarsel.device_id` blir liggende i nettleseren for alltid, også etter «Skru av».
2. Det finnes ingen samlet handling som fjerner alt.
3. Det er ikke verifisert om `DELETE /varsel-api/devices/{id}` også fjerner enhetens reiser. Dette må avklares før teksten under kan stå.

### Løsning

Én ny knapp i varselkortet, som kaller de eksisterende endepunktene i rekkefølge og deretter tømmer `localStorage`.

### T6 — Sletteknapp

Knappetekst:

> Slett alt

Bekreftelsesdialog:

> **Slette alt?**
> Dette sletter de registrerte reisene dine, varselabonnementet og den tekniske nøkkelen til denne nettleseren. Det kan ikke angres.
>
> [Avbryt] [Slett alt]

Bekreftelse etter utført sletting:

> Alt er slettet. Du kan registrere en ny reise når som helst.

### Rasjonale

Art. 17 nr. 1 bokstav b: den registrerte har rett til sletting når samtykket trekkes tilbake. Art. 7 nr. 3: tilbaketrekking skal være **like enkel** som å gi samtykke. Å registrere en reise tar fire felt og ett trykk; å fjerne alt bør ikke kreve at brukeren sletter reiser én for én og deretter finner «Skru av».

At `device_id` blir liggende i nettleseren er ikke bare formelt: den er nøkkelen til dataene, og verifisert sendes den både i URL-er og til `/varsel-api/client-log`. En bruker som tror hun har slettet alt, har fortsatt en aktiv nøkkel liggende.

### Oppgaver

| Oppgave | Rolle |
|---|---|
| Avklar om `DELETE /devices/{id}` også sletter reisene | Backend |
| Hvis ikke: la sletteflyten kalle begge, eller utvid endepunktet | Backend |
| «Slett alt»-knapp med bekreftelse, T6 | Frontend |
| `localStorage.removeItem('avviksvarsel.device_id')` etter vellykket sletting | Frontend |

---

## 5. A7 — Den avmerkevarede varianten

### Argument

Verifisert i kjørende tjeneste: på `/varsel/?brand=none` forekommer ordet «Entur» **ikke i synlig tekst** (`/entur/i.test(document.body.innerText)` = `false`). Sidetittelen er «Avviksvarsel». Manifestet `manifest-none.webmanifest` heter «Avviksvarsel» og har `display: standalone` — installert på hjemskjermen finnes ingen adresselinje, og dermed heller ingen `entur.io` å lese.

Samtidig er registrering fullt operativ: `POST /varsel-api/devices` tar `{device_id, subscription, brand}`. Merkevaren er en parameter, ikke en sperre.

En bruker kan altså registrere et pendlemønster, motta varsler og la appen ligge på telefonen uten på noe tidspunkt å få vite hvem som behandler opplysningene.

### Løsning

Velg **ett** av to. Begge lukker punktet.

**Alternativ 1 — anbefalt: én linje som alltid vises.**

### T7 — Avsenderlinje

Plassering: bunnen av siden, i alle merkevarevarianter.

> Tjenesten leveres av Entur AS. [Slik behandler vi opplysningene](#)

**Alternativ 2 — sperr registrering i varianten.** «Legg til reise» og «Slå på varsler» deaktiveres når `brand=none`, med teksten:

> Denne visningen er kun for demonstrasjon. Åpne tjenesten uten `?brand=none` for å registrere en reise.

### Rasjonale

Art. 13 nr. 1 bokstav a krever at identiteten til den behandlingsansvarlige oppgis. Det er ikke et designhensyn som kan vike for et annet designhensyn — det er et vilkår for at innsamlingen er lovlig.

Formålet med varianten, slik den er beskrevet i `demo/avviksvarsel/README.md`, er å vise konseptet «to an audience that should not read it as an Entur product». Det er et legitimt formål for en demonstrasjon. Det er ikke et grunnlag for å samle inn personopplysninger anonymt, og de to bruksmåtene må skilles.

Alternativ 1 er anbefalt fordi det bevarer varianten som demo, koster én linje, og ikke krever betinget logikk i to skjermer.

### Oppgaver

| Oppgave | Rolle |
|---|---|
| Beslutt alternativ 1 eller 2 | PM |
| Implementer valgt alternativ | Frontend |

---

## 6. Akseptansekriterier

Punktene er ferdige når alle utsagnene under er sanne, kontrollert i nettleser:

1. `/varsel` viser eksperimentforbehold (T1) og personvernlinje (T2) uten at brukeren må rulle eller klikke.
2. `/varsel/personvern` svarer 200 og inneholder alle punktene i T5.
3. «Legg til reise» viser T3 over knapperaden, i begge merkevarevarianter.
4. Varselkortet viser T4.
5. «Slett alt» finnes, og etter bruk er `localStorage.getItem('avviksvarsel.device_id')` lik `null`, og `GET /varsel-api/journeys?device_id=<den gamle>` returnerer tom liste.
6. På `/varsel/?brand=none` er enten «Entur AS» synlig i teksten, eller registrering deaktivert.
7. Ingen tekst i hakeparentes gjenstår i produksjon.

---

## 7. Forutsetninger som må bekreftes før publisering

Tekstene over inneholder fem påstander jeg **ikke** har kunnet verifisere fra frontenden. De må bekreftes, eller teksten justeres, før den publiseres.

| Påstand | Hvem bekrefter |
|---|---|
| Push-innholdet er kryptert i transport (RFC 8291 via `pywebpush`) | Backend |
| Varselteksten skrives av en språkmodell hos Google, i EU | Backend |
| Google Cloud drifter tjenesten på Enturs vegne, i EU, med databehandleravtale | Plattform |
| `DELETE /varsel-api/devices/{id}` sletter også enhetens reiser | Backend |
| Lagringstider: reiser, inaktivitet, beslutningslogg | PM + backend |

En påstand som ikke lar seg bekrefte, skal ut av teksten. En personvernerklæring som overdriver beskyttelsen er verre enn ingen.

---

## 8. Oppgavefordeling

| # | Oppgave | Rolle | Avhenger av |
|---|---|---|---|
| 1 | Beslutt verdier i hakeparentes og alternativ for A7 | PM | punkt 7 |
| 2 | Bekreft de fem forutsetningene | Backend + plattform | — |
| 3 | Avklar om enhetssletting også sletter reiser | Backend | — |
| 4 | Sletteflyt: kall begge endepunkter, tøm `localStorage` | Frontend + backend | 3 |
| 5 | `PersonvernNotis`-komponent, T1–T4 | Frontend | 1 |
| 6 | Rute `/varsel/personvern`, T5 | Frontend | 1, 2 |
| 7 | «Slett alt»-knapp med T6 | Frontend | 4 |
| 8 | Avsenderlinje eller registreringssperre, T7 | Frontend | 1 |
| 9 | Kontroller de sju akseptansekriteriene i nettleser | PM | 4–8 |
| 10 | Oppdater personvernvurderingen med teksten som faktisk står i appen | PM | 9 |

Oppgave 1, 2 og 3 er de eneste som blokkerer de andre. Ingen av dem krever personvernombudet.
