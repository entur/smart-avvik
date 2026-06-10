# Huskehjulet — E05-demo på ekte data

Scrollytelling-demo for «E05: Avvikslæring over tid (Huskehjulet)»
(docs/kix-docs/ki-eksperimenter/prototype-kandidater.md) — men i motsetning til
spesifikasjonen (som planla konstruerte hendelser) kjører denne på **ekte data**:
de 21 faktiske sporvekselfeilene ved Drammen stasjon, januar 2025 – januar 2026,
fra SIRI-SX, med ekte værdata (GHCN-D, Oslo Blindern) og resultatene fra
analysene i `docs/huskehjulet/LEARNINGS.md`.

## Kjøre

Ingen backend, ingen avhengigheter — åpne `index.html` i en nettleser:

```sh
open demo/huskehjulet/index.html
```

Vises best på storskjerm i plenum (jf. demokontekst i E05-spesifikasjonen),
fungerer også på mobil.

## Oppdatere dataene

Hendelseslisten er hardkodet i `index.html` (`INCIDENTS`-arrayen). Den ble
generert med spørringen i bunnen av denne filen — kjør den på nytt og lim inn
ved behov. Tallene i tekst og figurer (hyppighetsrater, backtest-tabellen,
median/p80) kommer fra `data/queries/huskehjul_census.sql`,
`huskehjul_backtest.sql` og `huskehjul_weather.sql`; resultatene står
dokumentert i filhodene og i `docs/huskehjulet/LEARNINGS.md`.

```sql
WITH target AS (
  SELECT id, name
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  WHERE name = 'Drammen stasjon'
),
inc AS (
  SELECT DISTINCT
    d.situationNumber, d.onset_ts, d.reopen_minutes, d.impact_minutes,
    d.summary, d.description
  FROM `ent-kixavvik-dev.disruptions.disruptions_initial` AS d
  LEFT JOIN `ent-kixavvik-dev.disruptions.categories` AS c
    ON  IFNULL(c.summary, '')     = IFNULL(d.summary, '')
    AND IFNULL(c.description, '') = IFNULL(d.description, '')
  CROSS JOIN UNNEST(d.affected_stops) AS sr
  JOIN target AS t ON t.id = sr
  WHERE ARRAY_LENGTH(d.affected_stops) <= 3
    AND c.category = 'infrastruktur/sporveksel'
)
SELECT i.*, w.tmin_c, w.snwd_mm
FROM inc AS i
LEFT JOIN `ent-kixavvik-dev.disruptions.hh_weather_blindern` AS w
  ON w.date = DATE(i.onset_ts, 'Europe/Oslo')
ORDER BY i.onset_ts;
```
