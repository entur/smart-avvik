-- The full category x line x hour-of-day impact lookup, loaded into an in-process
-- dict at cache-refresh time (see refresh_impact_lookup in main.py). Tiny table
-- (~3k rows), so the whole thing is pulled and indexed by (category, line, hour)
-- for the back-off lookup helper. Built offline by
-- data/queries/create_category_line_hour_impact.sql.
SELECT
  grain, category, line, hour,
  n_boardings, n_situations,
  cancel_rate, trouble_rate,
  delay_p50, delay_p90,
  base_trouble_rate, trouble_lift,
  exp_disruption_min, confidence,
  alert_score, alert_tier
FROM `disruptions.category_line_hour_impact`
