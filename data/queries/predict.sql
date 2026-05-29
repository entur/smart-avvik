CREATE OR REPLACE TABLE `disruptions.predictions`
AS
SELECT *
FROM
  ML.PREDICT(
    MODEL `ent-kixavvik-dev.disruptions.prediction_model`,
    (
      SELECT
        category,
        description,
        severity,
        affected_stops,
        affected_lines,
        year,
        month,
        day,
        dayofweek,
        hour,
        duration_minutes
      FROM `disruptions.training`
    ))
