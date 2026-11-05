-- Backfill the Rushvarsel notification rounds that fired before `togvarsel_rounds` existed.
--
-- One-off, run once per environment (done in dev and prd on 2026-09-02). The round table was
-- created by the deploy of PR #124/#125 — dev 2026-09-02 ~08:30 UTC, prd 2026-09-02 11:12 UTC — so
-- every round evaluated before that instant left no durable row: the marker store in the /varsel
-- state blob keeps a single date, and the only record of those rounds is the structured log event
-- `togvarsel_notify_round`, which lives in the GKE CLUSTER project (`ent-kub-dev` / `ent-kub-prd`,
-- not the app project), carries no Oslo date and expires after 30 days.
--
-- /rushhistorikk RECONSTRUCTS a round with no row by replaying the push policy on the forecast,
-- and files a would-have-been push under «rekonstruert» rather than «Varsler sendt». For the
-- three days below the reconstruction agrees with the log on every round — same decision, same
-- push/suppression — so this changes no verdict and no rate; it moves 7 (prd) / 7 (dev) pushes
-- from «rekonstruert» to «sendt», drops the «rekonstruert» chips, and gives `decided_at` the real
-- evaluation instant instead of the forecast's `computed_at`.
--
-- The rows are a COPY of the app's own decision record, not a re-derivation: every value is read
-- off the log event (Logs Explorer, project ent-kub-{dev,prd}):
--
--   resource.type="k8s_container"
--   resource.labels.namespace_name="smart-avvik"
--   jsonPayload.event="togvarsel_notify_round"
--
-- Field mapping — `d` is the Oslo date of the event timestamp; `decided_at` is that timestamp
-- (verified against a round the app did record: dev 2026-09-02 1200 has decided_at 10:00:38 and
-- the event is stamped 10:00:38.541Z); `sent` / `kind` / `reason` follow the event's shape (a push
-- carries `kind`, a suppression carries `decision`, which is the table's `reason`); the five
-- subscriber counts are the event's `matched_journeys` / `matched_devices` / `sent` / `error` /
-- `gone`, NULL on a suppression exactly as the app writes them; and `baseline_level` is what
-- `togvarsel_notify._effective_baseline` would have returned — the level the day's baseline round
-- pushed — NULL on a baseline round. No round below was untimely: every 0630 landed before 07:00
-- Oslo, every 0700 before 09:00, every 1200 before 14:00 and every 1400 before 18:00.
--
-- ALL missing rounds go in, suppressions included, not only the pushes. The page takes
-- `recording_started` as MIN(d) over this table, and a suppression left reconstructed after that
-- day would count as «Mangler runde» — the lost-write detector — for good.
--
-- Idempotent and environment-guarded: the app's own first-write-wins MERGE shape, so a row the app
-- already wrote is never touched (prd's 2026-09-02 1400, dev's 2026-09-02 1200 and 1400 are listed
-- for completeness and skipped), and every row names the project it belongs to, so running the
-- file against the wrong project — or against tst, which never pushes — inserts nothing.
MERGE `disruptions.togvarsel_rounds` AS t
USING (
  SELECT * EXCEPT (project)
  FROM UNNEST(ARRAY<STRUCT<
      project STRING, d DATE, issue STRING, level STRING, baseline_level STRING, sent BOOL,
      kind STRING, reason STRING, decided_at TIMESTAMP,
      matched_journeys INT64, matched_devices INT64, n_sent INT64, n_error INT64, n_gone INT64>>[
    -- ent-smartavvik-prd ------------------------------------------------------------------------
    ('ent-smartavvik-prd', DATE '2026-08-31', '0630', 'red',    NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-08-31 04:31:04.197 UTC', 21, 13,  9, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-08-31', '0700', 'red',    'red',    FALSE, NULL,        'no_change', TIMESTAMP '2026-08-31 05:01:28.982 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-prd', DATE '2026-08-31', '1200', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-08-31 10:00:44.288 UTC', 23, 14, 10, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-08-31', '1400', 'green',  'yellow', TRUE,  'all_clear', NULL,        TIMESTAMP '2026-08-31 12:01:27.397 UTC', 24, 15, 11, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-09-01', '0630', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-01 04:30:23.936 UTC', 38, 22, 13, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-09-01', '0700', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-01 05:01:21.796 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-prd', DATE '2026-09-01', '1200', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-01 10:01:42.859 UTC', 24, 15, 11, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-09-01', '1400', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-01 12:01:07.833 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-prd', DATE '2026-09-02', '0630', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-02 04:31:05.728 UTC', 39, 23, 14, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-09-02', '0700', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-02 05:00:53.184 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-prd', DATE '2026-09-02', '1200', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-02 10:02:33.021 UTC', 25, 15, 11, 0, 0),
    ('ent-smartavvik-prd', DATE '2026-09-02', '1400', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-02 12:00:48.823 UTC', NULL, NULL, NULL, NULL, NULL),  -- already recorded by the app
    -- ent-smartavvik-dev ------------------------------------------------------------------------
    ('ent-smartavvik-dev', DATE '2026-08-31', '0630', 'red',    NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-08-31 04:30:40.362 UTC',  9,  4,  3, 0, 0),
    ('ent-smartavvik-dev', DATE '2026-08-31', '0700', 'red',    'red',    FALSE, NULL,        'no_change', TIMESTAMP '2026-08-31 05:01:35.114 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-dev', DATE '2026-08-31', '1200', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-08-31 10:00:54.530 UTC',  9,  4,  4, 0, 0),
    ('ent-smartavvik-dev', DATE '2026-08-31', '1400', 'green',  'yellow', TRUE,  'all_clear', NULL,        TIMESTAMP '2026-08-31 12:01:17.800 UTC',  9,  4,  4, 0, 0),
    ('ent-smartavvik-dev', DATE '2026-09-01', '0630', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-01 04:31:05.098 UTC',  9,  4,  4, 0, 0),
    ('ent-smartavvik-dev', DATE '2026-09-01', '0700', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-01 05:02:02.695 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-dev', DATE '2026-09-01', '1200', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-01 10:01:43.466 UTC',  9,  4,  4, 0, 0),
    ('ent-smartavvik-dev', DATE '2026-09-01', '1400', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-01 12:00:50.103 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-dev', DATE '2026-09-02', '0630', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-02 04:30:36.266 UTC',  9,  4,  4, 0, 0),
    ('ent-smartavvik-dev', DATE '2026-09-02', '0700', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-02 05:01:05.861 UTC', NULL, NULL, NULL, NULL, NULL),
    ('ent-smartavvik-dev', DATE '2026-09-02', '1200', 'yellow', NULL,     TRUE,  'warning',   NULL,        TIMESTAMP '2026-09-02 10:00:38.541 UTC',  9,  4,  4, 0, 0),  -- already recorded by the app
    ('ent-smartavvik-dev', DATE '2026-09-02', '1400', 'yellow', 'yellow', FALSE, NULL,        'no_change', TIMESTAMP '2026-09-02 12:01:01.237 UTC', NULL, NULL, NULL, NULL, NULL)   -- already recorded by the app
  ])
  WHERE project = @@project_id
) AS s
ON t.d = s.d AND t.issue = s.issue
WHEN NOT MATCHED THEN
  INSERT (d, issue, level, baseline_level, sent, kind, reason, decided_at,
          matched_journeys, matched_devices, n_sent, n_error, n_gone)
  VALUES (s.d, s.issue, s.level, s.baseline_level, s.sent, s.kind, s.reason, s.decided_at,
          s.matched_journeys, s.matched_devices, s.n_sent, s.n_error, s.n_gone);
