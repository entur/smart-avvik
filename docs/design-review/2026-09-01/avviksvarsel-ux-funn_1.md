# Avviksvarsel + Rushvarsel — samlede UX-funn

Gjennomgått 1. september 2026: `/varsel/`, `/varsel/personvern/`, `/togmelding/`, mobil (375×812) og desktop (1440×900), pluss skjermbilder av «Mine reiser» med aktivt avvik og varselstabelen.

**Hvert punkt = én sak.** *Kursiv* = hvorfor. **Hvor** = verifisert ankerpunkt.

Alle ankerpunkter er sjekket mot de deployede filene, ikke gjettet: `varsel/assets/main-1zUbbOPj.js`, `varsel/assets/brand-BqyWhkcA.js`, `varsel/assets/personvern-BjG5BHMm.js`, `varsel/assets/{brand,main}-*.css`, `togmelding/assets/index-DbNFjWHP.js`, `togmelding/assets/index-6x-Pyml0.css`. Jeg oppgir greppbare strenger og CSS-klasser fremfor minifiserte symbolnavn, siden de sistnevnte ikke finnes i kilden.

**Håndverket er solid.** Linje gjennomgående, riktige tokens, `SkipToContent`, `fieldset`/`legend`, korrekte label-koblinger, `aria-live` på avvikslisten, beskrivende `aria-label` på ikonknapper, korrekt entall/flertall på `/varsel/`, fem presise valideringsmeldinger, `consent_text_version: 2026-08-31.1` sendes med hver registrering, all personverntekst sentralisert i ett delt modul, ingen kontrastfeil på `/varsel/`. Nesten alle funnene under er innhold og rekkefølge — ikke kode.

---

## Blokkerende — før eksterne brukere

**1. Kortet mangler ikke feltet — meldingen mangler handlingen.**
Kortet og pushen rendrer samme serverfelt `message`. Kortet fikk «Tog L1 07:18 er innstilt på grunn av mangel på personell.», pushen fikk «Tog L1 16:40 er innstilt. Ta tog RE11 16:44 i stedet.» Alert-objektet har `message`, `summary`, `mode`, `line`, `departure_time`, `cancelled`, `cancel_probability`, `delay_p50_min`, `delay_p90_min`, `latest_observation`, `alertworthy` — **ingen felt for alternativ avgang.** Alternativet lever bare inni den genererte strengen og forsvinner når modellen ikke tar det med.
*«Det viktigste først». Alternativet er hele grunnen til å åpne siden.*
**Hvor:** ikke frontend. Verifisert: verken `main-*.js` eller togmelding-bundelen inneholder «i stedet», «Ta tog» eller «mangler personell» — all varseltekst er serverside. Enten (a) gjør handling obligatorisk i prompten som skriver `message`, eller (b) legg til `alternative_departure` på alert-objektet i `/varsel-api/journeys` og render som egen linje ved siden av `av-alert-detail`. (b) er den robuste — et krav i en prompt kan ikke garanteres.

**2. Stoppsøket med «Alle» gir ubrukelige treff.** «Sandvika» → 30 forslag, «Sandvika (Troms)» seks ganger, «(Trøndelag)» fem. Filteret virker perfekt («Tog» + «Sandvik» = 1 treff) — problemet er at defaulten er «Alle».
*Feilforebygging (NN/g) og «Korteste vei til mål». Første steg i tjenestens eneste oppgave.*
**Hvor:** endepunktet `/varsel-api/stops` (grep i `main-*.js`) — dedupliser på stopp-ID, legg på kommune, sorter på relevans. Frontend: ikon per `mode` i listen.

**3. «Kjente avvik i dag» er 12 nesten like rader.** 11 av 12 er ordrett «Kortere tog / Færre vogner». Ingen linje, ingen stasjon, ingen tid.
*Synlighet av systemstatus (NN/g). Listen ser ut som en feil.*
**Hvor:** feltet `known_deviations` fra `/togmelding-api/today` (grep begge i togmelding-bundelen). Aggregering per type eller linje bør skje serverside; frontend rendrer `tm-kjente` / `tm-kjent`.

**4. «1 avganger».** Sju steder på siden og i pushen.
*Verdien «Lett å forstå».*
**Hvor:** grep `avganger i morgenrushet` i togmelding-bundelen — hardkodet flertall i `tm-kjent-count`. `/varsel/` gjør dette riktig alt: grep `aktivt avvik kan ramme` i `main-*.js` for mønsteret.

**5. `/togmelding/` har ingen vei tilbake og ingen registrering.** Eneste lenke er personvern.
*Brukerkontroll og frihet (NN/g). Rushvarsel er inngangen folk deler — nå er den en blindvei.*
**Hvor:** grep `tm-header` i togmelding-bundelen. Legg «Få dette som varsel» → `/varsel/` der, og en lenke tilbake i toppen.

**6. Feilmeldingen i skjemaet er ikke koblet til feltet.** Valideringen er én kaskade som returnerer én streng, rendret i én alert-boks nederst i modalen. Fra-feltet mangler `aria-invalid` og `aria-describedby`, og siden har ingen `aria-live`.
*WCAG 3.3.1 og 4.1.2. Skjermleserbruker som trykker «Lagre reise» får i dag ingenting.*
**Hvor:** grep `Velg et fra-stopp` i `main-*.js`. Endre returverdien fra `string` til `{field, message}`, render under riktig felt, flytt fokus dit. Gjelder alle fem: `Velg et fra-stopp`, `Velg et til-stopp`, `Fra- og til-stopp må være ulike`, `Velg tidsrom`, `Tidsrommet må starte før det slutter`, `Velg minst én ukedag`.

**7. Sletting skjer uten angremulighet.** Ingen `confirm` i koden, tre kort med identiske søppelikoner tett i tett — og sletting fjerner samtidig samtykkegrunnlaget.
*«Umulig å gå seg vill». Angre er raskere for de som mente det, og redning for de som ikke gjorde.*
**Hvor:** grep `Slett reise` i `main-*.js` (aria-label på ikonknappen i `av-journey`). Behold `DELETE /varsel-api/journeys/{id}`, legg angre-toast med tidsvindu før kallet sendes.

---

## Språk og tillit

**8. «Innstillinger» som kategorinavn** betyr *settings* i resten av Entur.
*Homonym som treffer nybegynneren hardest, og blir verre den dagen tjenesten får en innstillingsside.*
**Hvor:** grep `innstilt:` i togmelding-bundelen — kategorimappingen. Ettordsfiks til `Innstilte tog`.

**9. Tre registre for samme sannsynlighet.** Kortet: «Typisk 3–9 min forsinket» og «65 % sjanse for innstilling». Pushen: «kan bli 3 til 9 minutter forsinket». `/togmelding/`: «~2× tryggere enn snittet, røde ~3× verre». Velg prosent + «Typisk X–Y min» overalt.
*Verbale og relative sannsynlighetsuttrykk tolkes med stor spredning mellom personer — derfor binder f.eks. IPCC hvert uttrykk til et tallintervall. Multiplikatoren er ikke tolkbar i det hele tatt.*
**Hvor:** kortformatet: grep `Typisk ` og `sjanse for innstilling` i `main-*.js`. Pushteksten: serverside. «~2× tryggere»: serverfeltet `skill_note` (grep i togmelding-bundelen) — altså prompt/backend, ikke frontend.

**10. Terskelen for å vise sannsynlighet er hardkodet på 0.5.** Under 50 % vises forsinkelse i stedet for innstillingsrisiko — så en 45 % innstillingssjanse er usynlig i kortet, men kan godt ha blitt sendt som push.
*Kortet og varselet forteller da ulike historier om samme avgang.*
**Hvor:** grep `cancel_probability` i `main-*.js`. Vurder å vise begge, eller samkjøre terskelen med den som utløser push.

**11. Nivåskalaen bytter akse i midten:** «liten fare for forstyrrelser» → *«noe usikkert»* → «stor fare for forstyrrelser». → «moderat fare for forstyrrelser».
*Brukeren kan ikke plassere midtnivået mellom de ytre når det måler noe annet.*
**Hvor:** grep `noe usikkert` — finnes i **begge** bundlene (se 12).

**12. Trafikklys-labelene er duplisert og inkonsistente mellom appene.** `/varsel/` bruker `GRØNT / GULT / RØDT`, `/togmelding/` bruker `GRØNN / GUL / RØD`. Samme begrep, to bøyninger, to kopier.
*Duplisert innhold divergerer alltid. «Lett å forstå» krever ett vokabular.*
**Hvor:** grep `noe usikkert` i `main-1zUbbOPj.js` og `index-DbNFjWHP.js` — labelene ligger rett ved. Trekk ut i delt modul (personverntekstene ligger alt slik, i `brand-*.js` — samme mønster).

**13. Alle pushtitler er avkuttet** — «[dev] Asker stasjon → Lillestrøm s...», også i enkeltvisning. Tittel = faktum («Tog L1 07:18 er innstilt»), body = strekning + alternativ. `[dev]` spiser ~6 tegn.
*Tittelen er skannerflaten på låseskjermen. Nå brukes den på abonnementsnavnet, som brukeren alt kjenner.*
**Hvor:** serverside, der push-payloadens `title` settes. Verifisert at ingen varseltekst finnes i frontend-bundlene.

**14. Prognose og bekreftelse er ikke koblet.** 65 %-varselet og «er innstilt fordi operatøren mangler personell» om samme tog, samme time, som to løse varsler.
*Synlighet av systemstatus. Brukeren skal se én oppdatering, ikke to meldinger.*
**Hvor:** serverside push-payload. Sett samme `tag`/collapse-key, f.eks. `line + departure_time`, og innled med «Bekreftet:».

**15. Varselvolum: 6 varsler på én dag for 3 reiser** — før tjenesten har ekte brukere.
*Varslingstretthet: skrus kanalen av, forsvinner også varselet som betydde noe.*
**Hvor:** serverside utsendingslogikk. Tak per reise per dag, og ikke send prognose når bekreftelse er nært i tid.

**16. «... ankomst 18:54, som er senere enn planlagt»** — sett tallet: «34 min senere enn planlagt».
*Ingen skal regne i hodet på en låseskjerm.*
**Hvor:** prompt/serverside, samme sted som 13.

**17. Rushvarsel-pushen har to tall med ulikt nedslagsfelt:** «24 innstilte avganger så langt i dag. 1 avganger i ettermiddagsrushet er allerede innstilt.» Velg det som gjelder brukerens tidsrom.
**Hvor:** serverside, rushvarsel-payload. Flertallsfeilen er den samme som 4.

**18. Det røde panelet sier det samme to ganger:** `message` («Tog L1 07:18 er innstilt på grunn av mangel på personell.») og detaljlinjen («Tog L1 07:18 · Innstilt») overlapper fullstendig.
*Detaljlinjen har en jobb, men gjør den ikke når `message` alt har sagt alt.*
**Hvor:** grep `av-alert-detail` i `main-*.js` — la linjen falle bort når `message` inneholder både `line` og `departure_time`, eller la den bære spor / neste avgang i stedet.

**19. «Velg et fra-stopp.»** er systemspråk → «Du må velge hvor du reiser fra.»
**Hvor:** samme kaskade som 6.

**20. Taggen og teksten sier det samme:** taggen er kategorimappingen («Kortere tog»), teksten er `summary` («Færre vogner»).
**Hvor:** grep `tm-tag` i togmelding-bundelen. Skjul taggen ved overlapp, eller la `summary` bære linje og strekning.

**21. Meta-description på `/togmelding/` dupliserer ingressen.** Endres ingressen (se 30), blir de to ute av takt.
**Hvor:** `togmelding/index.html`, `<meta name="description">` — teksten finnes både der og i bundelen.

---

## Universell utforming

**22. `.tm-tag` faller under kontrastkravet.** `#626493` på `#D9DAE8`, 12 px / 600 = **4.02:1**, kravet er 4.5:1.
*WCAG 1.4.3. Målbart brudd.*
**Hvor:** `togmelding/assets/index-6x-Pyml0.css`, selektor `.tm-tag`. Egen klasse — bytt til Linjes tag-komponent eller til tokens.

**23. `.tm-kjent-count` er 10 px** og bærer faktisk informasjon (antall avganger). Minimum 12 px.
*Under alt Linje bruker.*
**Hvor:** samme CSS-fil, `.tm-kjent-count`.

**24. Trafikklysnivået er ikke bare farge — men begrepet mistes for skjermleser.** Badgen er `aria-hidden="true"`, mens pushen sier «gult nivå».
*1.4.1 er oppfylt visuelt (ord + setning + prosent, riktige Linje-tokens). Men samme bruker får vokabularet i varselet og mister det på siden.*
**Hvor:** grep `tm-badge` i togmelding-bundelen. Fjern `aria-hidden`, eller legg nivået i overskriften (se 25) og behold badgen dekorativ.

**25. H2 på `/togmelding/` er «Kl. 15:00 — ettermiddagsrushet», uten nivå.** → «Ettermiddagsrushet 15:00–18:00 — gult nivå».
*Overskriftsnavigasjon er hovedmåten skjermleserbrukere skanner en side. Sidens svar står i dag ikke i noen overskrift.*
**Hvor:** grep `tm-issue-level` — h2 ligger rett over i samme komponent.

**26. Verifiser `--fill-*-muted` i mørk modus** på nivåkortet.
*Muted fills er der kontrasten oftest ryker ved modusbytte, og dette er sidens viktigste flate.*
**Hvor:** `.tm-level-green` / `.tm-level-yellow` / `.tm-level-red` i togmelding-CSS — de setter kun `--tm-level-accent` og `--tm-level-tint`.

**27. Primærknappen er 133×32 px på mobil** (`eds-button--size-small`) på sidens eneste handling. → `size-medium`, full bredde.
*«Alle om bord» — rammer særlig brukere med redusert motorikk, og knappen er alt vanskelig å finne (se 29).*
**Hvor:** grep `av-section-head` i `main-*.js` (knappen står ved siden av h2 `Mine reiser`).

**28. Enter i skjemaet gjør ingenting.** Begge modalknappene er `type="submit"` uten `<form>`.
*Tastaturbruker må i dag tabbe forbi alle feltene til lagreknappen.*
**Hvor:** grep `av-form` / `av-form-actions` i `main-*.js`. Pakk feltene i `<form>`, «Lagre reise» `type="submit"`, «Avbryt» `type="button"`.

---

## Struktur og rekkefølge

**29. Hele førsteskjermen på mobil er forbehold og personvern.** «Legg til reise» ligger 1026 px ned — 1,26 skjermhøyder. 191 av 214 ord kommer før oppgaven starter.
*«Det viktigste først». Tekstene skal være der, men ikke i veien for handlingen.*
**Hvor:** rekkefølgen av barn i `<main>` på `/varsel/`: alert-boks → `av-intro` → `av-personvern-linje` → `av-status` → alert-boks (rushvarsel) → `av-section`. Flytt `av-personvern-notis` ut av `av-status` (se 41) og løft `av-section` opp.

**30. Ingressen på `/togmelding/` gjør tre jobber.** «Nye prognoser kl. 06:30, 07:00, 12:00 og 14:00 på hverdager; kl. 07:00 og 14:00 erstatter de to tidligere.» → «Oppdateres før morgen- og ettermiddagsrushet på hverdager», detaljene i en utvidbar «Om prognosen».
*Krever at leseren holder fire klokkeslett i hodet og mapper to mot to.*
**Hvor:** grep `Nye prognoser` i togmelding-bundelen, rendret i `.tm-intro`. Husk 21.

**31. Svaret på `/togmelding/` ligger tredje.** «Dagens rush er over» er en liten H2 i tredje kort. Nivået først og størst, prognoseplan og «sist oppdatert» som fotnote.
*Folk kommer for å få vite nivået nå.*
**Hvor:** kortrekkefølgen i togmelding-render: grep `Dagens rush er over`, `Ettermiddagsrushet`, `Kjente avvik i dag` — alle `tm-card`.

**32. «Sist oppdatert 22:14» mangler dato.** Bruk relativ tid primært.
*Ved en prognose er ferskhet hele poenget — 22:14 i dag og i forrige uke ser identisk ut.*
**Hvor:** grep `Sist oppdatert ` i togmelding-bundelen — formaterer feltet `generated_at`.

**33. Avviksalderen vises bare når `latest_observation` finnes.** Da får man «Sist observert: Skoppum, 25 min forsinket (kl 11:20)», som er utmerket. Uten den har kortet ingen tid i det hele tatt, slik som i skjermbildet.
*Synlighet av systemstatus. Et avvik om morgendagens 07:18-tog uten tidsstempel er vanskelig å stole på.*
**Hvor:** grep `Sist observert` / `av-alert-observed` i `main-*.js`. Legg fallback som viser når avviket ble mottatt.

**34. Blokkert-varsler-tilstanden nevner knapper som ikke finnes.** Teksten viser til «Skru av» og «Slett alt», men ingen rendres i den tilstanden.
*Brukeren leter etter noe som ikke er der.*
**Hvor:** `av-status` / `av-status-actions` / `av-status-note` i `main-*.js` — betinget rendering per varseltilstand.

**35. Aktivt kort har både tykk marineblå ramme og rødt fyll.** Velg én — helst fyllet, som er semantisk (`alertworthy` styrer alt `negative` vs `warning`).
**Hvor:** grep `av-alert-notification-target` i `main-*.js` og i `main-*.css`.

**36. Feilikonet (✕) står vertikalt sentrert til venstre for en skillelinje,** ikke i linje med tittelen. Ser ut som en alert-boks som har mistet layouten.
**Hvor:** CSS for `.av-alert-message` / `.av-alert-summary` / `.av-alert-detail` inne i Linjes alert-boks.

**37. «Var dette varselet nyttig?» ligger inne i det røde panelet.** Flytt under, lettere vekt.
*Rødt signaliserer hastegrad; tilbakemelding er administrasjon og arver nå alvoret.*
**Hvor:** feedback-komponenten rendres som siste barn i alert-boksen — grep `Var dette varselet nyttig` i `main-*.js`.

**38. Tidsrom på 15–16 timer** («07:00–22:00») gjør vinduet meningsløst som støydemper, og forverrer 15. Vurder maksbredde eller en vennlig merknad.
**Hvor:** valideringskaskaden (se 6) for regelen; `av-chip` i `av-journey-meta` for visningen.

**39. Desktop bruker 640 av 1440 px.** Greit som lesekolonne på `/varsel/`, men på `/togmelding/` blir avvikslisten en lang smal remse.
**Hvor:** `max-width:640px` — tre forekomster i hver CSS-fil (header-inner, main, footer). Gjelder begge apper; endre bare togmelding.

**40. Mangler «Fra/Til»-bytteknapp.** Sparer to søk for de som registrerer returen.
**Hvor:** modalen, mellom de to `Søk etter stopp`-feltene (`av-form`).

---

## Personvern

Alle personverntekstene — intro-linjen, notisen, modalteksten og hele personvernsiden — ligger i **ett** delt modul, `varsel/assets/brand-BqyWhkcA.js`. Endringer forplanter seg dermed automatisk. Selve *plasseringen* styres av klassene under.

**41. Flytt 54-ords-blokket om varselabonnement** til der varsler faktisk slås på, eller bak «Vis detaljer».
*GDPR art. 12/13 legger opp til informasjon når opplysningene innhentes. Modalteksten gjør dette helt riktig og skal ikke røres — feilen er at siden i tillegg forklarer hele mekanismen før brukeren har bestemt seg. Frigjør også førsteskjermen (se 29).*
**Hvor:** `.av-personvern-notis` (plassering, i `main-*.js`), teksten i `brand-*.js` — grep `varselabonnement`.

**42. «Slik behandler vi opplysningene» står tre ganger på samme side,** med samme mål. Behold footer + modal; gjør intro-lenken til «Hva vi lagrer om deg» eller fjern den.
*Skjermleserbrukere som lister lenker får tre identiske oppføringer.*
**Hvor:** `.av-personvern-linje` (intro), `.av-personvern-notis`, `<footer class="av-footer">`.

**43. Personvernsiden har bare én lenke (`pv-back`).** Legg til Datatilsynet, de åpne evalueringssidene den selv nevner, og Enturs hovedpersonvernerklæring.
*«Du kan klage til Datatilsynet» uten å si hvor er et halvt svar.*
**Hvor:** grep `Datatilsynet` og `evalueringssider` i `brand-*.js`; sidestrukturen er `pv-main` / `pv-section` i `personvern-*.js`.

**44. Ingen «Sist oppdatert» på en 495-ords erklæring.** Frontend sender alt `consent_text_version: 2026-08-31.1` med hver registrering — vis samme verdi på siden.
**Hvor:** grep `2026-08-31.1` i `brand-*.js`; render i `pv-intro`.

**45. Språket er godt — behold tonen.** Klarspråk, aktiv form, ærlig om språkmodellen og om at driftslogger ikke omfattes av «Slett alt». Fire grep, alle i `brand-*.js`:
- Snu seksjonsrekkefølgen brukerstyrt: Hva vi lagrer → Hvorfor → Hvem kan se det → Hvor lenge → Rettighetene dine → Rettslig grunnlag → Behandlingsansvarlig → Klage → Overføring. *(Seksjonene ligger som `pv-section` i rekkefølge; overskriftsstrengene er greppbare.)*
- Grep `fast periode`: «Driftslogger slettes automatisk etter en fast periode» → sett tallet. *Art. 13(2)(a) vil ha perioden eller kriteriet.*
- Grep `bokstav a`: legg klartekst i parentes — «art. 6 nr. 1 bokstav a (samtykke)».
- Grep `Behandlingsansvarlig`: del behandlingsansvarlig og personvernombud på to linjer. *To separate punkter i art. 13.*

**46. Ett spørsmål til personvernombudet — ikke fiks på magefølelse.**
Grep `evalueringssider` i `brand-*.js`: «Vurderingene ... vises også på åpne evalueringssider» er den mest inngripende setningen på siden og har minst plass. Strekning + avgangstid + vurdering er ikke nødvendigvis anonymt på tynne strekninger — «Møhlenpris → Bergen busstasjon, 07:00–09:00, Nei» er ikke mange personer. Eget avsnitt + lenke, og en vurdering av gjenidentifisering.

*(Samtykkegrunnlaget så jeg først som et problem, men `consent_text_version` sendes med hver registrering og sletting fjerner grunnlaget. Det er bedre håndtert enn de fleste — tar det av listen.)*

---

## Åpne spørsmål

**47. Hva skjer i stoppsøket hvis man skriver ferdig og klikker vekk uten å velge fra listen?**
Feltet mitt endte tomt, men jeg kjørte syntetiske events, så det kan være mitt oppsett.
**Hvordan sjekke:** skriv «Sandvika stasjon» i Fra, klikk utenfor uten å velge, se om teksten står. Tekst som forsvinner uten forklaring er en klassisk combobox-felle.

**48. Et varsel om 07:18-toget kom ca. 21:36 kvelden før.**
Er sendetiden bundet til reisens tidsvindu (07:00–23:00) eller til avgangen? Hvis kveldsvarsel er tilsiktet, si det i teksten («i morgen tidlig»). Hvis ikke, er det en bug.
**Hvordan sjekke:** serverside utsendingslogikk, samme sted som 15.

---

## Behold som det er

- «Ingen aktive avvik akkurat nå.» — nøytralt og faktisk, ikke en grønn frikjennelse. *En falsk frikjennelse koster mer tillit enn en falsk advarsel: den ene gjorde at noen dro tidlig, den andre at noen mistet toget.*
- «Tog L1 16:40 er innstilt. Ta tog RE11 16:44 i stedet.» — 12 ord, faktum + handling. Mal for resten.
- «Sist observert: Skoppum, 25 min forsinket (kl 11:20)» — konkret, etterprøvbar, riktig detaljnivå.
- Valideringen i modalen: fem presise meldinger, inkludert «Fra- og til-stopp må være ulike» og tidsromsjekk.
- `consent_text_version` på hver registrering, og all personverntekst i ett delt modul.
- Ukedagsformateringen («Man–Fre», «Hver dag») og defaults 07:00–09:00 / man–fre.
- Personverntekstens plassering i modalen, og språket på personvernsiden.
- `aria-live="polite"` på avvikslisten og `aria-label="Slett reise X til Y"`.
