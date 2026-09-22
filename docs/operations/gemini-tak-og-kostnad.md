# Gemini: tak, faktisk bruk og maks kostnad

Målt 2026-09-22 mot prd. Operatørnotat: hva som binder antall modellkall, hva de koster,
og hvilken spak som faktisk flytter dekning.


## Tak per miljø per døgn
| Flate | Grense i koden | Maks kall/døgn |
|---|---|---|
| /varsel browser (poller pass 1c) | `varsel._MAX_LLM_CALLS_PER_CYCLE` 64/syklus + 30 s klokke. INGEN time/døgn-tak | 64 x ~292 = 18 688 |
| /varsel webhook (klient-only par) | `_MAX_CALLS_PER_PASS` 32, 500/t, 2000/d | 2 000 |
| journey-alerts (pull) | `MAX_WORDINGS_PER_READ` 4, 30/min, 500/t, 2000/d | 2 000 |
| /meldinger replay | 1000/req, 1000/t, 5000/d | 5 000 |
| **SUM** | | **27 688** |

`notification_wording.BROWSER` er definert men aldri brukt i produksjonskode -> browser-stien har
ikke time/døgn-tak. Syklusperiode ~296 s (p50) -> 292 sykluser/døgn.

## Tokens per kall (målt i prd, 16.-21. sept, 19 391 varselverdige rader)
- systemprompt 5 373 tegn (fast)
- message_input: snitt 477, p90 704, maks 956 tegn
- svar: snitt 65, maks 220 tegn (`_MAX_CHARS` 300)
- `thinking_level: "low"` -> 0 thinking-tokens

## Kostnad (gemini-3.7-flash, $0.75/M inn, $3.75/M ut)
- MAKS, alle tak bundet: 54,8 M inn + 2,6 M ut = **$51/døgn/miljø = ~$1 520/mnd**
- begge miljøer: ~$102/døgn = **~$3 050/mnd**
- ett retry på hvert kall (attempts=2): ~$184/døgn begge miljøer

## Faktisk i dag (prd)
- 2 226 ferske kall på 7 døgn = **318/døgn = 1,1 % av taket = ~$12/mnd**
- `capped` = **20 rader på 7 døgn**. Gemini-taket binder ~3 ganger i døgnet.
- `not_selected` = 12 815 av 20 675 varselverdige rader (62 %) <- `_MAX_MESSAGES_PER_JOURNEY` (3/par)
  og `_pending_pushes`-utvalget, ikke Gemini-taket.

## Konsekvens for tall-i-nøkkelen
3,2-10x dagens volum = 1 000-3 200 kall/døgn = 4-12 % av taket = **$38-119/mnd/miljø**.
Overkommelig i dag. Dekningsargumentet gjelder 100k-populasjonen, ikke dagens prd.

Kilde pris: pricepertoken.com / søk 2026-09-22. Én kilde oppgir dette som introduksjonspris
til 31.12.2026, deretter $1.50/$7.50 — verifiser mot Google før budsjettering.

## Hva som faktisk binder under tall-i-nøkkelen
Slotten i `_MAX_MESSAGES_PER_JOURNEY` (3/par) brukes FØR `already_written`-sjekken (varsel.py:4842-4875),
så et cache-treff bruker en slot i dag. Tall-i-nøkkelen endrer derfor ikke DEKNING per par — de samme
3 togene får ordlyd, de blir bare ferske kall i stedet for treff.

Ordlyder valgt per syklus (prd, 7 døgn, 818 sykluser): snitt 9,4 / p50 4 / p90 22 / **p99 72 / maks 94**.
Under tall-i-nøkkelen er hver av dem et kall -> `_MAX_LLM_CALLS_PER_CYCLE` (64) binder på den
travleste ~1 % av syklusene. DET er spaken, ikke kostnaden.
Å heve 64 krever bredde: 64 = 16 workers x ~4 runder i et 10 s innslippsvindu (30 s pass - 20 s
`_WORST_CASE_CALL_S`). `_MAX_LLM_SECONDS_PER_CYCLE` 30 -> 45 s ligger innenfor ~120 s syklus-headroom.

Pull-lesningens `MAX_WORDINGS_PER_READ` = 4 begrenser FYLLRATE, ikke steady state: et cache-treff
serveres uansett posisjon (`_entry_state`), så bare nye ordlyder bruker taket.

## Etter omleggingen (2026-09-22)
`notification_wording.wording_key` bærer nå prediksjons-snapshotet, og takene er hevet:
`_MAX_LLM_CALLS_PER_CYCLE` 64 -> 128, `_MAX_LLM_SECONDS_PER_CYCLE` 30 -> 40 s, og alle fire
allowance-tallene i `notification_wording` 4x. «Faktisk i dag» over er dermed historikk:
forventet nytt nivå er **1 000-3 200 kall/døgn i prd** ($38-119/mnd/miljø), fortsatt 4-12 % av taket.

Én ting å forvente på første syklus etter deploy: hver `_messages`-oppføring er foreldreløs
(nøkkelformen er ny), så den syklusen omskriver alt og kan treffe 128-taket én gang. Det er normalt.

## Kortvinduet (målt, ikke anslått)
Mellom at `call_predictions_cache` stempler snapshotet og at `varsel_notify` har skrevet de nye
ordlydene, serverer /varsel-kortet og pull-lesningen den deterministiske linja i stedet for
modellens. Kortet går IKKE tomt: `JourneyCard.tsx` faller tilbake på operatørens sammendrag pluss
detaljlinja «Tog L1 07:18 · 88 % sjanse for innstilling», bygget klientside av de samme tallene
payloaden bærer — så den kan ikke motsi dem. Det er «malen er gulvet», etter planen.

Målt på prd over 12 sykluser 2026-09-22 (`poller_cycle.phases`):
`varsel_stops` + `varsel_recheck_service` + `notification_mirror` + `varsel_notify`
= **14,5-31 s, snitt ~20 s** av en ~296 s perioode, altså ~7 % av tida.

`varsel_stops` (~8 s, 40 % av vinduet) er flyttet OVER stempelet: den leser
`{rail,bus}_calls_planned_today` rett fra BigQuery og har ingen kobling til snapshotet i noen
retning. Vinduet er dermed ~12 s, ~4 %. Resten er `varsel_notify` selv — ordlyden kan ikke skrives
før matchingen er gjort — og krever omstrukturering, ikke en ny nøkkel.

Å nøkle på TALLENE i stedet for snapshotet ble målt og forkastet: det lukker bare 11-18 % av
vinduet (3,93 -> 3,51 med alle tall modellen kan bære forbi `_is_grounded`, -> 3,23 med bare de tre
prompten tillater), og koster et vedlikeholdt sett som må pinnes mot både `build_message_input` og
`_webhook_payload` i begge retninger. I de øvrige 82 % hadde den gamle teksten faktisk motsagt de
nye tallene, så malen er riktig svar der.
