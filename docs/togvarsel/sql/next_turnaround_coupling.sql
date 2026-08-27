WITH ends AS (
  SELECT c.operating_date, c.line, c.dsj, c.stop_place, c.aimed_arr, c.actual_arr, c.arr_delay_min,
    EXTRACT(HOUR FROM c.aimed_local) h
  FROM `ent-kixavvik-dev.togvarsel.calls` c
  WHERE NOT IFNULL(c.extra_journey,FALSE) AND c.actual_arr IS NOT NULL AND NOT c.cancelled
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.dsj ORDER BY c.seq DESC) = 1
),
starts AS (
  SELECT c.operating_date, c.line, c.dsj, c.stop_place, c.aimed_dep, c.actual_dep, c.dep_delay_min, c.cancelled
  FROM `ent-kixavvik-dev.togvarsel.calls` c
  WHERE NOT IFNULL(c.extra_journey,FALSE)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.dsj ORDER BY c.seq ASC) = 1
),
pairs AS (
  SELECT e.line, e.stop_place, e.h, e.arr_delay_min inbound_delay, s.dep_delay_min outbound_delay, s.cancelled outbound_cancelled,
    TIMESTAMP_DIFF(s.aimed_dep, e.aimed_arr, MINUTE) planned_turn,
    ROW_NUMBER() OVER (PARTITION BY e.dsj ORDER BY s.aimed_dep) rn
  FROM ends e JOIN starts s ON s.line = e.line AND s.stop_place = e.stop_place AND s.operating_date = e.operating_date
    AND s.aimed_dep > e.aimed_arr AND s.aimed_dep <= TIMESTAMP_ADD(e.aimed_arr, INTERVAL 45 MINUTE)
),
p AS (SELECT * FROM pairs WHERE rn = 1 AND EXTRACT(DAYOFWEEK FROM CURRENT_DATE()) IS NOT NULL)
SELECT line, COUNT(*) n_turns, ROUND(AVG(planned_turn),1) avg_planned_turn_min,
  ROUND(CORR(inbound_delay, outbound_delay),2) r_in_out,
  ROUND(AVG(IF(inbound_delay >= 10 AND NOT outbound_cancelled, CAST(outbound_delay >= 5 AS INT64), NULL)),3) p_out_late_given_in10,
  ROUND(AVG(IF(inbound_delay < 3 AND NOT outbound_cancelled, CAST(outbound_delay >= 5 AS INT64), NULL)),3) p_out_late_given_in_ontime,
  ROUND(AVG(IF(inbound_delay >= 10, CAST(outbound_cancelled AS INT64), NULL)),3) p_out_cx_given_in10,
  ROUND(AVG(IF(inbound_delay < 3, CAST(outbound_cancelled AS INT64), NULL)),3) p_out_cx_given_in_ontime,
  COUNTIF(inbound_delay >= 10) n_in10
FROM p GROUP BY line HAVING n_turns >= 2000 ORDER BY n_turns DESC
