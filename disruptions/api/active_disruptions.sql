-- Servable slice of disruptions.predictions_today for the /disruptions API endpoint.
--
-- main.py refreshes this into an in-process cache after every predictions_today rebuild
-- (i.e. on each new SX ingestion). The endpoint then turns each row's minutes-from-onset
-- estimate into "minutes from now" at request time (onset_ts + estimate - now), so the
-- cache holds the static onset + minute offsets and nothing time-relative is precomputed.
--
-- LIFECYCLE: onset -> reopen (trains running again) -> impact resolved (timetable back to
-- normal). reopen_ts is non-NULL once a reopening message ("Toget kjører igjen" / "Åpnet%")
-- has been observed (set upstream in disruptions_initial_today, carried through predict.sql).
-- That marks the REOPEN milestone, NOT the end of impact: residual delays/cancellations
-- typically linger after the line reopens, which is what the impact model estimates. So we
-- keep reopened disruptions in the feed and let the API flag them (reopen_ts -> `reopened`,
-- plus time-since-reopen) rather than dropping them here. The set is naturally bounded to
-- today's partition (it empties at the midnight rollover).
SELECT
  situationNumber,
  summary,
  description,
  category,
  onset_ts,
  reopen_ts,                  -- NULL while ongoing; reopening-message time once trains run again
  num_affected_stops,
  declared_window_min,
  -- reopen model: "trains running again in ~X min" (minutes from onset)
  pred_reopen_min,
  reopen_p50_min,
  reopen_p80_min,
  reopen_p90_min,
  -- impact model: "timetable fully back to normal in ~Y min" (>= reopen; minutes from onset)
  pred_impact_min,
  impact_p50_min,
  impact_p80_min,
  impact_p90_min
FROM `disruptions.predictions_today`
ORDER BY reopen_ts IS NOT NULL, onset_ts DESC   -- still-ongoing first, then reopened, newest onset first
