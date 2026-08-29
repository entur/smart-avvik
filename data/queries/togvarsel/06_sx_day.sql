-- disruptions.togvarsel_sx_day: per-day SX counters at the 06:30 / 09:00 / 12:00 cutoffs.
-- "fday" is the forecast day a message belongs to: messages created from the previous evening
-- 18:00 up to the cutoff of day D count toward D (fh is hours relative to D's midnight, so a
-- previous-evening message has fh < 0). The 07:00 / 14:00 cutoff counters and the category
-- counters live in 08_features_cat.sql — same fday/fh convention.
CREATE OR REPLACE TABLE `disruptions.togvarsel_sx_day` AS
WITH dates AS (SELECT d FROM `disruptions.togvarsel_days`),
ev AS (
  SELECT e.*,
    CASE WHEN created_h >= 18 THEN DATE_ADD(created_date, INTERVAL 1 DAY) ELSE created_date END AS fday,
    CASE WHEN created_h >= 18 THEN created_h - 24 ELSE created_h END AS fh
  FROM `disruptions.togvarsel_sx_events` e
),
agg AS (
  SELECT fday d,
    -- known by 06:30
    COUNT(DISTINCT IF(fh < 6.5, sn, NULL)) sx_n_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo, sn, NULL)) sx_oslo_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls='faerre_vogner', sn, NULL)) sx_fv_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='mangler_togsett', sn, NULL)) sx_mt_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls IN ('toget_staar','forsinket','forsinkelse_forventes'), sn, NULL)) sx_delay_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='personell', sn, NULL)) sx_pers_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='vaer', sn, NULL)) sx_vaer_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='vedlikehold', sn, NULL)) sx_vedl_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND dur >= 600, sn, NULL)) sx_long_0630,
    -- journeys already flagged for the AM / PM rush, known by 06:30
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 6.5 AND 9 AND operating_date=fday, j_ref, NULL)) sx_amj_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 15 AND 18 AND operating_date=fday, j_ref, NULL)) sx_pmj_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls IN ('innstilt') AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 6.5 AND 9 AND operating_date=fday, j_ref, NULL)) sx_amj_inn_0630,
    -- known by 09:00
    COUNT(DISTINCT IF(fh < 9, sn, NULL)) sx_n_0900,
    COUNT(DISTINCT IF(fh < 9 AND oslo, sn, NULL)) sx_oslo_0900,
    COUNT(DISTINCT IF(fh < 9 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_0900,
    COUNT(DISTINCT IF(fh < 9 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0900,
    COUNT(DISTINCT IF(fh < 9 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_0900,
    COUNT(DISTINCT IF(fh < 9 AND cause='mangler_togsett', sn, NULL)) sx_mt_0900,
    COUNT(DISTINCT IF(fh < 9 AND dur >= 600, sn, NULL)) sx_long_0900,
    COUNT(DISTINCT IF(fh < 9 AND oslo AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 15 AND 18 AND operating_date=fday, j_ref, NULL)) sx_pmj_0900,
    -- known by 12:00
    COUNT(DISTINCT IF(fh < 12, sn, NULL)) sx_n_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_1200,
    COUNT(DISTINCT IF(fh < 12 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 15 AND 18 AND operating_date=fday, j_ref, NULL)) sx_pmj_1200,
    -- previous evening (18-24) only
    COUNT(DISTINCT IF(fh < 0, sn, NULL)) sx_preveve,
    COUNT(DISTINCT IF(fh < 0 AND oslo AND cls='innstilt', sn, NULL)) sx_preveve_inn
  FROM ev GROUP BY fday
)
SELECT dt.d, agg.* EXCEPT(d) FROM dates dt LEFT JOIN agg ON agg.d = dt.d
