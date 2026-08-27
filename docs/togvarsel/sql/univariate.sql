WITH f AS (SELECT * FROM `ent-kixavvik-dev.disruptions.togvarsel_features` WHERE weekday AND NOT holiday AND am_n > 300 AND pm_n > 300),
u AS (
  SELECT name, val, am_t5, pm_t5, am_bad, pm_bad FROM f, UNNEST([
    STRUCT('et_t5_0630' AS name, et_t5_0630 AS val), ('et_t10_0630', et_t10_0630), ('et_cx_0630', et_cx_0630), ('et_cxj_0630', et_cxj_0630), ('et_md_0630', et_md_0630), ('et_max_0630', et_max_0630),
    ('prev_eve_t5', prev_eve_t5), ('prev_eve_cancel', prev_eve_cancel), ('prev_pm_t5', prev_pm_t5), ('night_t5', night_t5),
    ('sx_n_0630', sx_n_0630), ('sx_oslo_0630', sx_oslo_0630), ('sx_fv_0630', sx_fv_0630), ('sx_mt_0630', sx_mt_0630), ('sx_inn_0630', sx_inn_0630), ('sx_big_0630', sx_big_0630),
    ('sx_delay_0630', sx_delay_0630), ('sx_infra_0630', sx_infra_0630), ('sx_pers_0630', sx_pers_0630), ('sx_vaer_0630', sx_vaer_0630), ('sx_vedl_0630', sx_vedl_0630), ('sx_long_0630', sx_long_0630),
    ('sx_amj_0630', sx_amj_0630), ('sx_pmj_0630', sx_pmj_0630), ('sx_amj_inn_0630', sx_amj_inn_0630), ('sx_preveve', sx_preveve), ('sx_preveve_inn', sx_preveve_inn),
    ('et_t5_0900', et_t5_0900), ('et_t5_last_0900', et_t5_last_0900), ('sx_inn_0900', sx_inn_0900), ('sx_big_0900', sx_big_0900), ('sx_infra_0900', sx_infra_0900), ('sx_pmj_0900', sx_pmj_0900), ('sx_long_0900', sx_long_0900),
    ('et_t5_1200', et_t5_1200), ('et_t5_last_1200', et_t5_last_1200), ('sx_pmj_1200', sx_pmj_1200), ('sx_inn_1200', sx_inn_1200), ('sx_big_1200', sx_big_1200),
    ('et_t5_1400', et_t5_1400), ('et_t5_last_1400', et_t5_last_1400),
    ('month', month), ('dow', dow)
  ])
),
q AS (SELECT name, APPROX_QUANTILES(val, 4)[OFFSET(3)] q75 FROM u GROUP BY name)
SELECT u.name, COUNT(*) n, ROUND(AVG(val),2) mean, q.q75,
  ROUND(CORR(val, am_t5),3) r_am, ROUND(CORR(val, pm_t5),3) r_pm,
  ROUND(AVG(IF(val > q.q75, CAST(am_bad AS INT64), NULL)),3) p_am_bad_top, ROUND(AVG(IF(val <= q.q75, CAST(am_bad AS INT64), NULL)),3) p_am_bad_rest,
  ROUND(AVG(IF(val > q.q75, CAST(pm_bad AS INT64), NULL)),3) p_pm_bad_top, ROUND(AVG(IF(val <= q.q75, CAST(pm_bad AS INT64), NULL)),3) p_pm_bad_rest,
  COUNTIF(val > q.q75) n_top
FROM u JOIN q USING (name) GROUP BY 1, q.q75 ORDER BY ABS(r_pm) DESC
