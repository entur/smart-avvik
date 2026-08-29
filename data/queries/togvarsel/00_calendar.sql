-- disruptions.togvarsel_calendar: THE calendar for the togmelding — Norwegian public holidays
-- (plus the rail-quiet 24/12 and 31/12) and the low-season flag, one row per date. The single
-- source that the training chain (07_features.sql), the serving query
-- (disruptions/api/togvarsel_features_today.sql) and the rollover probe in togvarsel.py all
-- read, replacing the twin hardcoded lists that could drift apart or silently expire (§13 of
-- the findings doc).
--
-- EXTEND YEARLY: append next year's holidays and low-season ranges, bump `horizon`, re-run this
-- file, and run data/queries/copy_runtime_to_prd.sh. Past `horizon` the serving query ERRORs
-- loudly instead of silently treating a holiday as a forecast day, and retrain.sh refuses to run
-- once the calendar has under 60 days left.
DECLARE horizon DATE DEFAULT '2027-05-17';  -- the last maintained date

CREATE OR REPLACE TABLE `disruptions.togvarsel_calendar` AS
WITH hol AS (
  -- Weekday public holidays. Weekend holidays are deliberately absent (the weekday gate runs
  -- first everywhere), and the list starts where the training data does: last_recorded carries
  -- VYG rail only from 2025-04-10, so earlier days are never eligible.
  SELECT d FROM UNNEST([DATE '2025-04-17', '2025-04-18', '2025-04-21', '2025-05-01', '2025-05-17', '2025-05-29', '2025-06-09', '2025-12-24', '2025-12-25', '2025-12-26', '2025-12-31', '2026-01-01',
    '2026-04-02', '2026-04-03', '2026-04-06', '2026-05-01', '2026-05-14', '2026-05-25',
    '2026-12-24', '2026-12-25', '2026-12-26', '2026-12-31', '2027-01-01',
    '2027-03-25', '2027-03-26', '2027-03-29', '2027-05-01', '2027-05-06', '2027-05-17']) d
)
SELECT
  cal_d AS d,
  hol.d IS NOT NULL AS holiday,
  -- Low season: July + Christmas + Easter weeks.
  (EXTRACT(MONTH FROM cal_d) = 7
   OR cal_d BETWEEN '2025-12-22' AND '2026-01-02'
   OR cal_d BETWEEN '2025-04-14' AND '2025-04-21'
   OR cal_d BETWEEN '2026-03-30' AND '2026-04-06'
   OR cal_d BETWEEN '2026-12-21' AND '2027-01-03'
   OR cal_d BETWEEN '2027-03-22' AND '2027-03-29') AS low_season
FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2024-08-01', horizon)) AS cal_d
LEFT JOIN hol ON hol.d = cal_d
