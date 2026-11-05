# Replacing the BigQuery poll with Pub/Sub or Kafka (2026-09-24)

Question from the product owner, 2026-09-24: can the poller stop polling BigQuery and listen to
SIRI-SX and SIRI-ET messages instead, preferably on Google Cloud Pub/Sub, otherwise Kafka? How do
other teams consume these feeds, Team Selvbetjening's Entur app in particular? Would it shorten
the time from a message is published until we can send a notification, and would it save
BigQuery cost?

Everything below is measured against prd on 2026-09-24 unless stated otherwise: the poller's own
job history in `INFORMATION_SCHEMA.JOBS_BY_PROJECT` for the seven days to 2026-09-24, live probes
of `ent-anshar-prd.realtime.siri_realtime_{sx,et}_production` with the query cache off, and the
source of anshar and its consumers read through Kompass. Cost of the investigation: 14 jobs, the
largest 113 MB, under $0.01 in total.

## Short answer

- **Possible, yes.** Anshar publishes every SIRI element to Pub/Sub topics in `ent-anshar-prd`
  and to Kafka on Aiven, and four internal services already consume them. Access is a small
  Terraform change in a repo that already grants our service accounts BigQuery access.
- **It would not shorten the lag by much, because the lag is ours.** The 1 to 6 minute stage we
  measured on 2026-09-08 and attributed to anshar's load batching is BigQuery's streaming buffer.
  Fresh rows have `_PARTITIONTIME` NULL for a few minutes, and every live query we run filters on
  `DATE(_PARTITIONTIME) = CURRENT_DATE()`, so the poller cannot see them. One `OR _PARTITIONTIME
  IS NULL` per query removes the stage at no measurable cost. After that fix, Pub/Sub would gain at
  most one poll interval, because the ~50 s rebuild that follows detection is the same either way.
- **It would not save BigQuery cost unless ET moves too, and ET is a firehose.** SX reads are
  $0.11/day of prd's $6.97/day. The ET scans are $5.4/day, and the ET topic cannot be filtered by
  operator: 8.0 M messages a day, of which the trains we serve are 75 k.
- **Faster detection costs rebuilds, whatever the transport.** Each rebuild bills ~0.9 GB of
  `ML.PREDICT`. Seeing SX as it lands would take rebuilds from ~250/day to a ceiling of ~750/day,
  $1.4/day to ~$4/day per environment, unless a rebuild-spacing cap is added.

## What the poller does today

`main.poll_for_changes` runs `last_ingestion_time.sql` once a minute: `MAX(ingestionTime)` over
today's SIRI-SX partition. When the value moves, `main._rebuild_all` runs in this order: the ET
actuals snapshot `rail_calls_today` (on its own TTL), the SX snapshot `disruptions_initial_today`,
Gemini categorisation, `predict.sql`, the planned-calls snapshots (TTL 60/120 min), the removed
trains, `predict_calls.sql` for rail and bus, then the matching and notification passes. The
cycle takes 50 to 63 s and its period is ~296 s at p50. The period was read as "how often the
upstream SIRI-SX ingestion timestamp moves"; the section below shows it is how often BigQuery
flushes its streaming buffer.

## Finding 1: the lag is the streaming buffer, not anshar

Anshar does not load BigQuery in batches. The tables are Pub/Sub **BigQuery subscriptions**
(`siri-sx-to-bigquery-subscription` and its ET and VM siblings in
`entur/anshar-deployment-config`, `terraform/siriPubsub.tf`, `use_topic_schema = true`), which
write through the Storage Write API within seconds of publication.

Google's documentation on streaming into ingestion-time partitioned tables says: "Recently
streamed rows to an ingestion time partitioned table temporarily have a NULL value for the
`_PARTITIONTIME` pseudocolumn. For such rows, BigQuery assigns the final non-NULL value in the
background, typically within a few minutes. In rare cases, this can take up to 90 minutes." There
is no SLA for how long rows stay in the `__UNPARTITIONED__` partition.

Measured with `bq query --nouse_cache`, UTC:

| Sample | Newest SX row visible through `DATE(_PARTITIONTIME) >= CURRENT_DATE()` | Newest SX row present (`OR _PARTITIONTIME IS NULL`) |
|---|---|---|
| 20:20:09 | 20:15:09 | 20:19:17 |
| 20:22:07 | 20:19:17 | 20:21:21 |

At the first sample the buffer held 3 SX rows and 7 519 ET rows, the newest ET row recorded one
second earlier, while the newest VYG row the poller could see was 90 s old. `bq show` on the
table exposes the same thing as `streamingBuffer.estimatedRows`.

This is the "`MAX(ingestionTime)` frozen for ~5 min, then jumping" pattern from the 2026-09-08
measurement, and it explains why "~4 of 5 polls find no change": the value can only move when
BigQuery flushes. The memory note that blamed anshar's commit cadence was wrong on the cause and
on the conclusion that "the only lever is upstream".

**The buffer scan is free in practice.** The probe with and without the buffer clause bills the
same 10 MB minimum (14 KB processed). `rail_calls_today`'s VYG scan with the clause billed 113
MB, the same as without it. The SX buffer was 2 KB, the ET buffer 23 MB.

**Where the clause belongs.** Only in the live-today queries: `last_ingestion_time.sql`,
`rail_calls_today.sql`, `rail_calls_planned_today.sql`, `bus_calls_planned_today.sql`,
`disruptions_today.sql`, `disruptions_bus_initial_today.sql` and both ET reads in
`togvarsel_features_today.sql`. Not in `disruptions_as_of.sql`, which replays history. Rows that
are in the buffer at query time get today's partition when flushed, so a row is never counted
twice across the two branches of the filter. **Applied in the same change as this document**
(branch `feat/streaming-buffer-visibility`), together with `POLLER_MIN_REBUILD_SPACING_S`, a
120 s floor between rebuild starts in `main.poll_for_changes`.

## Finding 2: how anshar publishes, and who listens

`entur/anshar` (`routes/pubsub/PubsubTopicRoute.java`, `PubsubAvroPublisherRoute.java`) splits
each ServiceDelivery into one message per `PtSituationElement` or `EstimatedVehicleJourney` and
fans it out three ways at once:

- **Pub/Sub, Avro-JSON**, schema-validated topics in `ent-anshar-{dev,tst,prd}`:
  `siri.situation_exchange`, `siri.estimated_timetables`, `siri.vehicle_monitoring`. The BigQuery
  tables are built from these topics with the topic schema, so a message has exactly the columns
  we query today, `ingestionTime` included.
- **Pub/Sub, XML**: `xml.situation_exchange`, `xml.estimated_timetables`, `xml.facility_monitoring`.
- **Kafka** on the Aiven prod-int cluster: anshar has write ACLs on `siri-realtime*`
  (`entur/aiven-kafka-infrastructure`, `kafka-users/RoR/anshar.tf`). The SX topic is
  `siri-realtime-sx-v2-production`; the ET topic name was only seen as a placeholder.

The serializer strips every header before publishing, so the messages carry **no attributes**.
Pub/Sub subscription filters work on attributes only, so an ET subscription is the whole
national feed. Measured for 2026-09-23: 7 971 996 ET rows, of which 75 118 were VYG; 2 308 SX
rows, of which GCO fanned out 1 591 across county codespaces, NSB 202, RUT 168, GOA 29.

Consumers, by team:

| Team | Service | Reads | How |
|---|---|---|---|
| ROR (team-ruter-reiseplanlegger, owns anshar) | ukur | ET + SX | Pub/Sub, subscription in its own project on anshar's topic |
| ROR | realtime-cache (feeds OTP) | ET + SX | Pub/Sub XML topics, one subscription per pod |
| ROR | dumuzid | ET | Pub/Sub Avro topic, own project, `roles/pubsub.subscriber` |
| ROR | kishar, siri-parquet | VM, all | Pub/Sub |
| Inntektsmodell | sjs-producer | ET + SX | Kafka `siri-realtime-*-v2-production` |
| **Selvbetjening** | Entur app BFF | deviations | **ukur**, not a bus |

The Entur app does not consume Pub/Sub or Kafka. `entur/entur-clients`, `bff/domains/ukur/src/
client.ts`, subscribes per line at `${ENTUR_API_HOST}/realtime-deviations/v1/subscription` with
`minimumDelay: PT5M` and a push address on its own `deviationReceiver` function, and turns ukur's
HTTP push into FCM. Ukur is ROR's deviation-subscription service: it holds the subscriptions in
memory, matches every anshar message against them, and POSTs matches to the subscriber. Whether
its subscription API is open to a team outside ROR is not documented anywhere I could find.

**Access precedent.** Ukur and dumuzid create their subscription in their own project against
`projects/ent-anshar-prd/topics/...`; that needs `pubsub.topics.attachSubscription` on the topic,
which ROR grants as `roles/pubsub.subscriber`. `anshar-deployment-config/terraform/main.tf` already
carries `google_bigquery_dataset_iam_member` entries for `application@ent-smartavvik-dev` and
`application@ent-smartavvik-prd`, so the ask is one `google_pubsub_topic_iam_member` per SA in the
same file, and a `google_pubsub_subscription` in our own `terraform/`. Dev would attach to the prd
topic: dev SIRI is not usable, as Team Inntektsmodell's Confluence pages note for their own dev
setup. Kompass's `list_pubsub_subscriptions` is denied to us on `ent-anshar-*`, so the live
subscription list could not be read.

## Finding 3: the cost anatomy

`application@` query jobs, seven days to 2026-09-24, on-demand rate $6.25/TiB:

| prd, per day | $ | Jobs/day | Notes |
|---|---|---|---|
| ET scans | 5.44 | ~150 | `rail_calls_planned_today` 2.20 (also reads GTFS), `rail_calls_today` 1.48, `bus_calls_planned_today` 0.65, togvarsel's two ET feature reads 1.11 |
| `ML.PREDICT` rebuild chain | 1.30 | ~246 | `call_predictions_today` 0.89, `call_predictions_bus_today` 0.26, `predictions_today` 0.09, categories 0.06 |
| SX reads | 0.11 | ~1 450 | probe 0.07 over ~1 200 runs, `disruptions_initial_today` 0.03, `disruptions_bus_initial_today` 0.01 |
| Everything else | 0.12 | | decision log, outcomes, loads |
| **Total** | **6.97** | 5 400 | |

Dev spends $8.58/day on the same shape; its extra is `rail_calls_planned_today` and the togvarsel
reads, not the rebuilds. One rebuild bills ~0.9 GB, about $0.006: the sum of the per-cycle
CTASes divided by their 1 720 runs.

Two consequences:

1. **Streaming SX saves nothing.** The probe and the two SX snapshots are $0.11/day.
2. **Streaming ET is where the money is, and it does not pay.** The $5.4/day of ET scans would be
   replaced by a consumer that holds the day's VYG and bus calls itself and writes them to a table
   `ML.PREDICT` can read, fed from a subscription that delivers 8 M messages a day at roughly
   4.8 KB each, the average row size of the ET table (29.4 TB over 6.08 G rows). That is of the
   order of 38 GB/day, and Pub/Sub delivery is priced per TiB, so of the order of $1 to $2/day per
   environment. Treat that as an estimate to check against the pricing page. Net, roughly $3/day
   per environment, for a stateful consumer with replay, deduplication and restart semantics that
   the current CTAS chain gets for free. Kafka has no per-byte delivery fee but needs SASL
   credentials from `#talk-utviklerplattform`, schema-registry Avro decoding and a Python consumer
   in a repo that has none.

## Latency, stage by stage

| Stage | Today | After the buffer clause | With SX on Pub/Sub |
|---|---|---|---|
| Operator to anshar `ingestionTime` | p50 0 s, p90 3 s | same | same |
| Row visible to us | 1 to 6 min, no SLA | ≤ 60 s poll | seconds |
| Detection to push (`_rebuild_all`, `varsel_notify` last) | 31 to 76 s, median 47 s | same | same, unless scoring is narrowed to the affected trains |
| **Typical end to end** | **~5 min** | **~1.5 to 2 min** | **~1 min** |

The step Pub/Sub removes is the poll wait, worth 30 s on average at a 60 s interval. The rebuild
is the floor in both columns, and shortening it means scoring only the trains a message names
instead of the whole day's timetable, which is a change to `predict_calls.sql`, not to the
transport.

**Rebuild count is the cost knob, and detection speed turns it.** With the buffer visible the
probe moves on almost every tick: all-operator SX arrives every ~37 s, and GCO, which we serve,
is 70 % of it, so filtering the probe by `participantRef` does not help. The loop is one thread,
poll then rebuild, so the ceiling is one rebuild per 60 s sleep plus ~55 s cycle, about 750 a day
against 246 today: $1.4/day becomes ~$4/day per environment. Pub/Sub has the identical ceiling
unless events are coalesced. Either design needs a minimum spacing between rebuilds, or a split
where a relevant SX message triggers a narrow scoring pass and the full rebuild keeps a slower
clock.

## Options

| | Change | Lag gain | BigQuery saving | Build |
|---|---|---|---|---|
| A | `OR _PARTITIONTIME IS NULL` in the eight live-today reads, plus a rebuild-spacing cap | 1 to 6 min → ≤ 1 min detection | none; +$0 to +$2.6/day depending on the cap | one PR, SQL only |
| B | SX Avro-JSON topic on Pub/Sub, consumer thread triggers the rebuild | ≤ 30 s over A | $0.11/day | topic IAM PR to ROR, subscription in our Terraform, `google-cloud-pubsub`, a consumer thread, dev attached to prd |
| C | B plus ET, replacing the ET CTASes | as B; ET cancellations seen in seconds instead of on the 60/120 min TTL | ~$5.4/day minus ~$1 to $2 delivery | a stateful firehose consumer, 8 M msg/day, and a write path into a table `ML.PREDICT` reads |
| D | Kafka instead of Pub/Sub for B or C | as B or C | as C without the delivery fee | SASL credentials, schema registry, Avro, a Python Kafka client |
| E | Subscribe to ukur like the Entur app does | seconds, for line-level SX and ET deviations | none | depends on ROR opening `realtime-deviations/v1`; ukur pushes deviations, not the full ET, so it cannot feed the feature tables |

Not pursued: anshar's public SIRI Lite endpoint with `requestorId` returns only changes since the
last call and needs no IAM, but it is still polling, in XML, and gains nothing over A.

## Recommendation

Do A. It is the one change that moves the number the question is about, and it is a filter on
eight queries. Pair it with a minimum rebuild spacing, 120 s is a reasonable first value, then
re-measure end to end with the 2026-09-08 method. Revisit B only if a product case for
sub-minute alerts appears, and then together with a narrow scoring pass, because without one the
gain over A is a poll interval. C and D are not worth their build for ~$3/day per environment.

## Open questions for Team ROR

- Is the Kafka ET topic `siri-realtime-et-v2-production`, and is `#talk-utviklerplattform` the
  right place to ask for consumer credentials?
- Is ukur's subscription API at `realtime-deviations/v1` available to a team outside ROR, and
  under what terms?
- If B is ever wanted: would ROR accept a `google_pubsub_topic_iam_member` for our two
  `application@` service accounts on `siri.situation_exchange` in `anshar-deployment-config`,
  next to the dataset grants already there?

## Corrections this triggers

- The memory note `sx-to-notification-lag` was rewritten on 2026-09-24 with the buffer as the
  cause. The related paragraph in `CLAUDE.md` ("the cycle period is set by how often the upstream
  SIRI-SX ingestion timestamp moves") is reworded in the same change: after A the period is set
  by the spacing floor.
- The 2026-08-10 Dataplex note counts CTAS jobs per cycle as the lineage cost driver in dev; a
  higher rebuild rate after A raises that line too, in dev only.

## Method and reproducibility

Buffer visibility, run twice two minutes apart:

```sql
-- bq query --nouse_legacy_sql --nouse_cache
SELECT 'buffer' AS src, COUNT(*) AS n, MAX(ingestionTime) AS max_ts, CURRENT_TIMESTAMP() AS now
FROM `ent-anshar-prd.realtime.siri_realtime_sx_production` WHERE _PARTITIONTIME IS NULL
UNION ALL
SELECT 'today partition', COUNT(*), MAX(ingestionTime), CURRENT_TIMESTAMP()
FROM `ent-anshar-prd.realtime.siri_realtime_sx_production` WHERE DATE(_PARTITIONTIME) = CURRENT_DATE();
```

Billed bytes per variant: run each probe, then `bq show --format=prettyjson -j <jobId>` and read
`statistics.query.totalBytesBilled`.

Cost attribution per query shape, one environment:

```sql
SELECT
  CASE WHEN destination_table.table_id LIKE 'anon%' OR destination_table.table_id IS NULL
       THEN CONCAT('select: ', REGEXP_EXTRACT(query, r'(?s)^\s*(?:--[^\n]*\n\s*)*([^\n]{0,70})'))
       ELSE CONCAT('table: ', REGEXP_REPLACE(destination_table.table_id, r'_[0-9a-f]{8,}$', '')) END AS what,
  COUNT(*) AS jobs,
  ROUND(SUM(total_bytes_billed)/POW(1024,4)*6.25/7, 2) AS usd_per_day
FROM `ent-smartavvik-prd`.`region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND job_type = 'QUERY' AND state = 'DONE' AND error_result IS NULL
  AND user_email LIKE 'application@%'
GROUP BY what ORDER BY usd_per_day DESC;
```

Sources read: `entur/anshar` `routes/pubsub/PubsubTopicRoute.java`, `PubsubAvroPublisherRoute.java`,
`SiriAvroJsonSerializer.java`; `entur/anshar-deployment-config` `terraform/siriPubsub.tf`,
`terraform/main.tf`; `entur/ukur` `functional_description.md`, `technical_description.md`,
`terraform/main.tf`; `entur/dumuzid` `terraform/pub_sub.tf`; `entur/realtime-cache` README;
`entur/sjs-producer` `service/kafka/ConsumerService.kt`; `entur/entur-clients`
`bff/domains/ukur/src/client.ts`; `entur/aiven-kafka-infrastructure`
`kafka-users/RoR/anshar.tf`; Google Cloud docs, "Streaming data into BigQuery" and "Storage
Write API", sections on ingestion-time partitioning.
