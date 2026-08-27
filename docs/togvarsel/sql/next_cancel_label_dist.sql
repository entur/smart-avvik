SELECT COUNT(*) n, ROUND(AVG(am_cancel),4) mean_am_cx, ROUND(AVG(pm_cancel),4) mean_pm_cx,
  COUNTIF(am_cancel >= 0.04) n_am_cx4, COUNTIF(am_cancel >= 0.06) n_am_cx6, COUNTIF(pm_cancel >= 0.04) n_pm_cx4, COUNTIF(pm_cancel >= 0.06) n_pm_cx6,
  ROUND(CORR(am_cancel, pm_cancel),2) r_amcx_pmcx, ROUND(CORR(sx_pmj_0630, pm_cancel),2) r_pmj0630_pmcx, ROUND(CORR(sx_pmj_0900, pm_cancel),2) r_pmj0900_pmcx, ROUND(CORR(sx_pmj_1200, pm_cancel),2) r_pmj1200_pmcx,
  ROUND(CORR(sx_mt_0630, pm_cancel),2) r_mt_pmcx, ROUND(CORR(et_cx_0630, pm_cancel),2) r_etcx_pmcx, ROUND(CORR(sx_inn_0630, pm_cancel),2) r_inn_pmcx, ROUND(CORR(sx_amj_inn_0630, am_cancel),2) r_amjinn_amcx,
  ROUND(AVG(IF(sx_pmj_0630 >= 5, CAST(pm_cancel>=0.05 AS INT64), NULL)),3) p_pmcx5_given_pmj5, ROUND(AVG(IF(sx_pmj_0630 < 5, CAST(pm_cancel>=0.05 AS INT64), NULL)),3) p_pmcx5_rest, COUNTIF(sx_pmj_0630 >= 5) n_pmj5
FROM `ent-kixavvik-dev.disruptions.togvarsel_features` WHERE weekday AND NOT holiday AND am_n>300 AND pm_n>300
