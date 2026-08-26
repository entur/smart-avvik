-- Exact traveller-visible alert snapshots used to enrich isolated PostHog feedback.
-- Append-only and environment-local. Never add this table to runtime copy scripts.
CREATE TABLE IF NOT EXISTS `disruptions.varsel_feedback_contexts`
(
  context_id STRING NOT NULL,
  context_date DATE NOT NULL,
  persisted_at TIMESTAMP NOT NULL,
  display_source STRING NOT NULL,
  displayed_text STRING,
  detail_text STRING,
  observation_text STRING,
  situation_number STRING,
  operator STRING,
  line STRING,
  departure_time TIMESTAMP,
  journey_context JSON,
  message_input JSON,
  llm_provider STRING,
  llm_model STRING,
  prompt_version STRING,
  message_status STRING,
  prediction JSON,
  policy JSON,
  delivery JSON
)
PARTITION BY context_date
CLUSTER BY context_id
OPTIONS(partition_expiration_days=90);
