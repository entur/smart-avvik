-- Free-text feedback comments, kept out of PostHog: PostHog keeps events for 7 years and has no
-- per-event deletion, while a comment can hold anything a traveller types. Keyed by the PostHog
-- event's event_id and partitioned like varsel_feedback_contexts, so a comment expires with the
-- context it was written about. Append-only and environment-local. Never add this table to runtime
-- copy scripts.
CREATE TABLE IF NOT EXISTS `disruptions.varsel_feedback_comments`
(
  event_id STRING NOT NULL,
  context_id STRING NOT NULL,
  response_id STRING NOT NULL,
  revision INT64 NOT NULL,
  context_date DATE NOT NULL,
  submitted_at TIMESTAMP NOT NULL,
  comment STRING NOT NULL
)
PARTITION BY context_date
CLUSTER BY event_id
OPTIONS(partition_expiration_days=45);
