# Reisevalg-varsel: Kan vi si «ta toget i dag» – og ha rett?

*Oppsummering av mulighetsstudien, juli 2026. Skrevet for lesere som ikke har sett
den opprinnelige presentasjonen. Teknisk dokumentasjon: `LEARNINGS.md` i samme mappe.*

## Ideen som ble undersøkt

Hver morgen velger pendlere mellom bil og tog – som regel på vane, ikke på fakta. Ideen
(«Proaktivt reisevalg-varsel») er et vennlig dytt i reiseappen i det øyeblikket valget tas:
når det bygger seg kø på E18 inn mot Oslo *og* togene går som normalt, sier appen ifra:

> «Bedre å ta toget i dag. Det bygger seg kø på E18. Togene Lier–Drammen–Oslo S går som
> normalt. Du sparer ca. 20 min. Ledig plass på Lier innfartsparkering.»

Premisset i presentasjonen var at Entur allerede sitter på dataene som trengs: sanntids
togpunktlighet, åpne kø-data fra Statens vegvesen, og innfartsparkering. Denne studien
testet det premisset mot virkeligheten – med to års faktiske data for strekningen
Lier–Drammen–Oslo S, og ved å bygge en fungerende prototype.

## Konklusjonen i tre setninger

1. **Togsiden av varselet kan vi levere i dag.** Vi vet i sanntid om togene går, og vi kan
   til og med *forutsi* innstillinger før de skjer.
2. **Veisiden kan vi ikke levere ærlig med åpne data.** De åpne kø-dataene er for trege og
   for upresise til å bære et varsel – men det finnes to gode veier videre.
3. **Selv med perfekte data er øyeblikket varselet lever av, sjeldent:** «kø på veien og
   tog i rute» inntreffer bare et par prosent av morgenene – oftest ødelegger samme uvær
   begge deler samtidig.

## Hva vi fant, løfte for løfte

Varselet i presentasjonen fremsetter fire påstander. Slik står de seg:

**«Det bygger seg kø på E18» – kan ikke sies pålitelig i dag.**
De eneste åpne kø-dataene er timevise trafikktellinger fra Statens vegvesen. Vi hentet
340 000 timesmålinger fra 10 målepunkter langs E18 og testet om de kan avsløre kø-morgener.
Beste resultat: av 10 morgener metoden flagget som «kø», var 6 reelle – en treffsikkerhet på
0,60, der vi på forhånd satte 0,8 som minstekrav for å dytte folk ut av bilen. Verre er
tidspunktet: tallene publiseres først 2–3 timer etter at timen er omme. Kø som oppstår i
morgenrushet blir synlig i dataene rundt kl. 10:30 – *etter* at pendlerne har reist.
Prototypen er derfor bevisst innstilt slik at dette signalet aldri utløser et varsel.

**«Togene går som normalt» – kan sies i dag, men er sant sjeldnere enn antatt.**
Dette er den delen Entur faktisk behersker: avgangstavla i sanntid pluss kix-avviks egne
prediksjoner (som kan varsle *forventede* innstillinger før de er et faktum). Men to års
historikk viser at «togene går som normalt» – målt som ingen innstillinger og maks fem
minutter forsinkelse inn til Oslo S – bare stemmer på 52–58 % av vanlige hverdagsmorgener.
Omtrent annenhver morgen ville varselet måtte tie fordi toget selv ikke holder løftet.

**«Du sparer ca. 20 min» – kan ikke tallfestes uten måledata for kjøretid.**
Regnestykket er stramt: toget bruker ca. 28,5 minutter Lier–Oslo S, bilen ca. 36 minutter
uten kø, og det går tog hvert 30. minutt. Med normal ventetid må bilen tape rundt 7,5
minutter til kø før toget vinner – og rundt 22 minutter hvis du akkurat mistet et tog.
«Du sparer 20 minutter» beskriver altså en uvanlig ille morgen, ikke en typisk kø-dag.
Og for å si tallet i det hele tatt trengs målt kjøretid på veien, som i dag ikke er
åpent tilgjengelig.

**«Ledig plass på innfartsparkeringen» – finnes ikke som åpen datakilde.**
Sanntids ledighet finnes bare inne i Bane NORs egen parkeringsapp. Åpent finnes kun antall
plasser totalt. Varselet kan si «det finnes innfartsparkering på Lier», ikke «det er ledig».

## Hvor ofte ville varselet faktisk slått til?

Vi spolte tilbake gjennom 380 hverdagsmorgener (desember 2024–mai 2026, 299 av dem
«rene» – utenom ferier og planlagt sommerarbeid på banen) og spurte: hvor ofte var veien
unormalt dårlig *samtidig som* toget gikk som normalt?

- Med en **tenkt perfekt kø-detektor**: 6–7 av 299 morgener – rundt **2 %**, altså 4–7
  morgener i året.
- På **to av tre** morgener med reell kø var også toget rammet – snø og uvær treffer vei
  og bane samtidig. De morgenene det frister mest å si «ta toget», er ofte de morgenene
  toget ikke fortjener det.

Dette betyr ikke at ideen er verdiløs – et varsel som treffer riktig 4–7 ganger i året kan
fortsatt bygge tillit hvis det aldri bommer. Men det er et *sjeldent* hendelsesvarsel, ikke
et daglig verktøy. Hvis intensjonen egentlig var en daglig, ærlig sammenligning av bil og
tog («slik ser reisen ut i dag»), er det et annet – og oftere anvendelig – produkt, som den
samme løsningen kan levere. Det valget bør tas eksplisitt.

## To veier til gode kø-data

1. **Statens vegvesens reisetidsdata (DATEX II).** Målte kjøretider på E18 hvert 5. minutt –
   akkurat det varselet trenger. Krever registrering og godkjenning, og dataene lagres ikke
   historisk hos SVV, så innsamling må starte den dagen tilgangen er på plass. Søknaden er
   ferdig utformet (`datex-access-request.md`) og bør sendes straks.
2. **Enturs egne busser som «målebiler»** – studiens mest lovende funn. Rutebussene
   (linje 160 og 250) kjører selv E18 mot Oslo hver morgen, og den faktiske kjøretiden
   deres finnes allerede i Enturs sanntidsdata – ferskt på minuttet, med to års historikk,
   uten avtaler med noen. 21 000 morgenturer er analysert. Forbehold: bussene bruker
   kollektivfelt på deler av strekningen og merker derfor køen mindre enn bilistene.

## Hva som er bygget og hva det kostet

En fungerende prototype er på plass i kix-avvik-tjenesten: et API som for strekningen
Lier–Oslo S svarer «ta toget i dag» / «ingen kø-gevinst» / «ikke regn med toget» /
«utenfor morgenrushet» / «usikkert» – med norsk tekst og ærlighetsregelen innebygd: er
datagrunnlaget for svakt, sier den «usikkert» i stedet for å gjette. Den dagen bedre
veidata er på plass, kan varselet skrus på uten ombygging.

Hele undersøkelsen – datainnhenting, analyse og prototype – kostet under **3 kroner** i
skyanalysekostnader, mot en ramme på 1 000 kroner.

## Anbefaling

1. **Send DATEX-søknaden nå** – ventetiden er ukevis, og hver uke uten tilgang er
   reisetidsdata som er tapt for alltid.
2. **Bygg bussmåler-signalet** – det kan gjøre varselet mulig uten å vente på noen.
3. **Ta produktvalget**: sjeldent hendelsesvarsel («i dag er dagen») eller daglig
   reisesammenligning? Dataene bærer det siste bedre enn det første.
4. **Ikke lov «ledig plass» eller «du sparer X minutter»** før datakildene som kan holde
   løftene, faktisk er på plass. Et bomvarsel er billig én gang – men tilliten varselet
   skal bygge, tåler få av dem.
