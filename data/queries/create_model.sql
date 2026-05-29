CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.prediction_model`
OPTIONS (
  MODEL_TYPE = 'BOOSTED_TREE_REGRESSOR',
  INPUT_LABEL_COLS = ['duration_minutes'],
  NUM_TRIALS = 100,
  HPARAM_TUNING_ALGORITHM = 'VIZIER_DEFAULT',
  HPARAM_TUNING_OBJECTIVES = ['R2_SCORE']
) AS
SELECT * FROM `ent-kixavvik-dev.disruptions.training_cat`
WHERE  
  duration_minutes > 0
  AND duration_minutes <= 600
