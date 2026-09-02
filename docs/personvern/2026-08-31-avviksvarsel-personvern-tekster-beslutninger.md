# Beslutningsnotat: personverntekster i Avviksvarsel

**Dato:** 31. august 2026
**Grunnlag:** `docs/personvern/prd-avviksvarsel-personvern-tekster.md` (A2, A3, A4, A7)
**Plan:** `docs/plans/2026-08-31-001-feat-varsel-personvern-tekster-plan.md`
**Formål:** dette notatet er kilden personvernvurderingen oppdateres fra. Det sier hva som faktisk
ble publisert, hvilke påstander teksten hviler på, og hvilke tre fakta som fortsatt må bekreftes
utenfor kodebasen.

---

## 1. Hva som ble bygget

| PRD-punkt | Løsning | Hvor |
|---|---|---|
| A2 — informasjon på flaten | T1 eksperimentforbehold, T2 personvernlinje, T3 registreringstekst, T4 varselkort | `demo/avviksvarsel/src/components/PersonvernNotis.tsx`, montert i `App.tsx`, `AddJourneyModal.tsx`, `VarselStatus.tsx` |
| A2 — personvernside | T5 på `/varsel/personvern/` | `demo/avviksvarsel/personvern/index.html` + `src/personvern-page.tsx` (egen Vite-inngang) |
| A3 — samtykke | T3 er hele tiltaket; ingen avkrysningsboks. Versjonen av teksten logges og lagres | `src/personvern.ts` (`CONSENT_TEXT_VERSION`), `disruptions/api/varsel.py` (`consent_text_version`) |
| A4 — sletting | T6 «Slett alt» + `DELETE /varsel-api/devices/{device_id}/data` | `src/components/VarselStatus.tsx`, `disruptions/api/varsel.py` |
| A7 — avmerkevaret variant | T7 avsenderlinje, synlig i alle merkevarevarianter | `App.tsx` (bunntekst), og på `/togmelding` |

Alle publiserte tekster har **én kilde**: `demo/avviksvarsel/src/personvern.ts`. Komponentene
importerer derfra; ingen norsk streng er hardkodet i en komponent. `/togmelding` kan ikke importere
modulen — derfor er tekstene duplisert der (`demo/togmelding/src/personvern.ts`) og sikret med en
paritetstest (`disruptions/api/test_personvern_texts.py`). *Grunnen* endret seg 2026-09-01, ikke
ordningen: siden var ren, committet HTML uten byggesteg fram til da, og er nå en egen npm-app hvis
Docker-steg bare kopierer sin egen katalog — så et relativt import inn i `../avviksvarsel/src/`
ville løst seg lokalt og feilet i CI.

**Samtykkeversjon ved lansering:** `2026-08-31.1`.

Versjonen bumpes hver gang en tekst en registrerende bruker ser endres. Det er **testhåndhevet**,
ikke overlatt til hukommelsen: `demo/avviksvarsel/test/personvern.test.mjs` pinner en sha256-hash
av registreringstekstene ved siden av versjonen, og en endring av teksten uten at begge flyttes
feiler suiten. Samme resonnement som `llm_message.PROMPT_VERSION`.

---

## 2. Beslutninger

### A7 — Alternativ 1 (avsenderlinje), ikke Alternativ 2 (sperret registrering)

Avsenderlinjen «Tjenesten leveres av Entur AS» vises i **alle** merkevarevarianter, også
`?brand=none`. Valgt framfor å deaktivere registrering i den avmerkevarede varianten: den bevarer
varianten som demonstrasjon, koster én linje, og oppfyller art. 13 nr. 1 bokstav a.

**Konsekvens som må være kjent:** `demo/avviksvarsel/README.md` lovet tidligere at ordet «Entur»
ikke forekommer noe sted under `?brand=none`. Det gjelder ikke lenger, og skal ikke gjelde — den
nye invarianten er at *sidens tekst* navngir avsenderen, mens varslene, manifestet og ikonene
fortsatt er umerket. README er oppdatert.

### Lagringstid — teksten beskriver faktisk oppførsel, det bygges ingen sletterutine

PRD-utkastet foreslo at reiser «fjernes automatisk etter 90 dager uten aktivitet», og lot antall
dager for beslutningsloggen stå åpent. **Ingen av delene er sanne i dag.** En gjennomgang av `varsel.py` finner
ingen utløpsmekanisme overhodet — ingen TTL, ingen opprydding, intet `last_seen`-felt, ingen
periodisk sletting. `_MAX_DEVICES` / `_MAX_JOURNEYS` er tak som avviser nye skrivinger, ikke
utkastelse. Verken `varsel_decisions` eller `varsel_decision_outcomes` har
`partition_expiration_days`.

Etter PRD-ens egen regel — «en påstand som ikke lar seg bekrefte, skal ut av teksten» — sier den
publiserte teksten derfor det som faktisk gjelder: reiser lagres til brukeren sletter dem,
beslutningsloggen lagres så lenge eksperimentet pågår og identifiserer ikke enheten, og
tilbakemeldinger lagres i inntil 45 dager (den ene verifiserte lagringstiden i kodebasen —
`create_varsel_feedback_contexts.sql` pinner `partition_expiration_days=45`).

**En faktisk sletterutine er utsatt arbeid**, ikke en del av denne leveransen. Vurder den mot
art. 5 nr. 1 bokstav e (lagringsbegrensning): ubegrenset lagring av et pendlemønster er teksten nå
ærlig om, men ærlighet er ikke det samme som lovlighet.

### Google og EU — bekreftet, og testsikret

To påstander i T5 ble bekreftet av produkteier og står i teksten:

- varselteksten skrives av en språkmodell hos Google i EU, som får vite hvilken strekning og
  avgang varselet gjelder;
- Google Cloud drifter tjenesten på Enturs vegne, i EU. «Overføring ut av EØS» ble dermed satt til
  «Behandlingen skjer i EU/EØS» — **rettet 1. september 2026**, se neste avsnitt.

Begge hviler på **Helm-verdier, ikke kodestandarder**: kodens standard er `provider="auto"`, som
ville forsøkt Claude på `global` først. `disruptions/api/test_personvern_texts.py` pinner derfor at
hvert miljø setter `VARSEL_LLM_PROVIDER: gemini` og **eksplisitt** `VERTEX_GEMINI_LOCATION: eu`.
Eksplisitt er poenget: effektiv lokasjon er `VERTEX_GEMINI_LOCATION or VERTEX_LOCATION or "eu"`, og
dev og prd setter allerede `VERTEX_LOCATION: global` — å fjerne Gemini-pinnen ville flyttet
behandlingen ut av EU uten at noe ble rødt. Samme test pinner `POSTHOG_HOST` i prd, som
PostHog-i-EU-setningen hviler på.

### Rettelse 1. september 2026 — Web Push-transporten manglet i EØS-vurderingen

«Overføring ut av EØS» sto opprinnelig som den ene setningen **«Behandlingen skjer i EU/EØS»**. Den
var usann, og motsa dessuten sin egen nabo to seksjoner opp — der teksten allerede sa at «tjenesten
som leverer varselet til telefonen din ser ikke innholdet — det er kryptert». En tjeneste som ikke
kan lese innholdet er nettopp en tjeneste som *får* noe.

Årsaken er en systematisk skjevhet i vurderingen, ikke en glipp i ordlyden: vi veide de
databehandlerne **Entur velger** — Google Cloud, Vertex, PostHog — og alle tre ligger i EU. Web
Push har én til, og den velges av **nettleseren**: hvert varsel POSTes til et endepunkt
abonnementet oppgir, driftet av nettleserleverandøren (FCM for Chrome/Android, Mozilla for Firefox,
Apple for Safari/iOS). De endepunktene ligger rutinemessig utenfor EØS, og vi kan verken velge dem
bort eller pålegge dem lokasjon — de følger av brukerens nettleservalg.

Teksten sier nå at behandlingen *vi selv styrer* skjer i EU/EØS, at varselet må via nettleserens
varslingstjeneste for å nå fram, at den kan ligge utenfor EØS, og hva den faktisk får: den
krypterte meldingen og leveringsadressen. Overføringen er reell, men innholdet er kryptert
ende-til-ende av Web Push-protokollen (RFC 8291), så det som krysser grensen er et chiffertekst-
blob pluss abonnementsadressen — ikke strekningen, avgangstiden eller varselteksten.

To ting dette **ikke** avgjør, og som hører hjemme i personvernvurderingen: hvilket
overføringsgrunnlag som gjelder for abonnementsadressen (den er en identifikator for nettleseren,
og den krysser grensen i klartekst), og om Entur regner nettleserleverandøren som databehandler
eller som ren transportør. Formuleringen er valgt så den er sann uansett hvilket av de to svarene
som lander.

### Sletting — ett endepunkt, ikke en klientløkke

`DELETE /varsel-api/devices/{device_id}/data` fjerner enhetsposten, alle enhetens reiser og de
reisenes dedup-nøkler under **én** låsing og **én** lagring. Valgt framfor at klienten løkker over
de eksisterende per-objekt-endepunktene: den varianten er ikke atomisk, koster N rundturer, og
etterlater `_notified`-nøkler i den persisterte tilstanden.

Endepunktet er idempotent (ukjent id gir 200 med null slettinger) og gjør **null I/O** på
ukjent id — hver mutasjon holder låsen over en blokkerende GCS-skriving, så et uautentisert
endepunkt som lagret på hver tilfeldige UUID ville vært gratis skriveforsterkning. Slettingen
kvitteres med én strukturert logglinje (`event="varsel_delete_all"`).

**«Skru av» og «Slett alt» er ikke det samme**, og teksten skiller nå: «Skru av»
(`DELETE /devices/{id}`) fjerner varselabonnementet og **beholder** reisene — det er dokumentert
oppførsel, nå pinnet av sin aller første test. T4 er omformulert fra PRD-utkastet av samme grunn:
utkastet sa at den tekniske nøkkelen slettes ved «Skru av», noe som ikke stemmer (nøkkelen ligger
igjen både i nettleseren og på hver reisepost). Den slettes ved «Slett alt».

---

## 3. Hva «Slett alt» ikke rekker

Teksten er skrevet slik at den er sann også for dette. Bekreftelsesdialogen ramser opp nøyaktig de
tre tingene som slettes (reisene, varselabonnementet, den tekniske nøkkelen) — hold den
oppramsende ved framtidige tekstendringer; en kortere «dette sletter alt vi har om deg» ville vært
usann.

| Spor | Overlever sletting? | Dekket av teksten |
|---|---|---|
| Reiser, enhetspost, dedup-nøkler | Nei — slettes | T6 |
| `localStorage`-nøkkelen | Nei — slettes | T6 |
| Driftslogger (push-utsendelser med teknisk nøkkel og varseltekst; slettekvitteringen) | Ja, til loggens egen slettefrist | T5 «Hva vi lagrer» + «Hvor lenge» |
| Beslutningsloggen i BigQuery | Ja — men uten `device_id`; reisen er en `journey_digest` | T5 «Hvor lenge» |
| Tilbakemeldinger (PostHog, BigQuery-kontekster) | Ja, inntil 45 dager | T5 |
| Mellomlagrede varseltekster i minnet | Ja, til situasjonen dør | Ikke nevnt — de er nøklet på reisedefinisjonen, ikke enheten, og deles med andre registreringer av samme reise |

En reise som registreres på nytt gir **samme** `journey_digest`, så rader i beslutningsloggen kan
knyttes sammen på tvers av en sletting. Det er tilsiktet (loggen er nøklet på reisedefinisjonen,
ikke på personen), men bør stå i personvernvurderingen.

---

## 4. Fire ting som må bekreftes utenfor kodebasen

Disse lot seg ikke avgjøre fra repoet. Teksten er formulert så den er sann uansett svar, men
svarene bør inn i personvernvurderingen — og to av dem kan kreve en tekstjustering.

1. **Versjonering på GCS-bøttene for tilstandsfilen.** Bøttenes versjonering/soft-delete er ikke
   deklarert i `terraform/`. Beholdes tidligere generasjoner av `varsel_state.json`, kan reiser
   gjenopprettes etter «Slett alt», og T6 må enten få en livssyklusregel bak seg eller mykere
   ordlyd.
2. ~~**Faktisk lagringstid for driftslogger**~~ — avklart 2026-09-02: `gcloud logging buckets
   list` viser 30 dager på `_Default`-bøtta (europe-west1) i både `ent-kixavvik-dev` og
   `ent-kixavvik-prd` (den 400-dagers, låste bøtta er `_Required`, som bare holder
   admin-aktivitetslogger). T5 «Hvor lenge» sier nå «30 dager».
3. **PostHogs konfigurerte lagringstid** i EU-instansen. «Inntil 45 dager» er verifisert for
   BigQuery-kontekstene; bekreft at PostHog ligger på eller under samme grense.
4. **Overføringsgrunnlaget for Web Push-endepunktet** (nytt 1. september 2026, se rettelsen i
   avsnitt 2). Avklar om nettleserleverandørens varslingstjeneste regnes som databehandler eller
   som transportør, og hvilket grunnlag som dekker abonnementsadressen — det eneste som krysser
   grensen i klartekst. Teksten er formulert så den er sann uansett svar, men svaret hører hjemme i
   vurderingen.

---

## 5. Én ting til, som ikke er en forutsetning men et faktum

T5 sier at vurderingene av varslene — med strekning og avgangstid, men uten noe som identifiserer
enheten — vises på **åpne** evalueringssider. Det er ikke en formulering man velger; det er hva som
gjelder. `/historikk` og `/meldinger` er dekket av SSO-porten i `sso.py`, men `SSO_REQUIRED` står
til `"false"` i alle miljøer, og siden redigeringen 22. august viser `/historikk`-kortene fra- og
til-navn ved siden av avgangstiden, mens `/meldinger` gjengir prosa som rutinemessig navngir begge
endene av en reise.

Å utelate dette ville gjort mottakerlisten usann fra dag én. **Skrus SSO-porten på**, bør denne
setningen snevres inn til Entur-ansatte i samme endring — ingen annen mekanisme holder den
publiserte mottakerlisten i takt med portens tilstand.
