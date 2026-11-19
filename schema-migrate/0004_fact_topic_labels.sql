-- Preserve the model-era topic label alongside topic_id and model hash. The ID
-- alone cannot be interpreted using a future model's label map.

ALTER TABLE hourly_sentiment_facts
    ADD COLUMN IF NOT EXISTS topic_label TEXT NOT NULL DEFAULT 'Unassigned';

ALTER TABLE hourly_sentiment_fact_snapshots
    ADD COLUMN IF NOT EXISTS topic_label TEXT NOT NULL DEFAULT 'Unassigned';

CREATE INDEX IF NOT EXISTS hourly_sentiment_facts_topic_lookup_idx
    ON hourly_sentiment_facts
    (symbol, topic_label, bucket_hour DESC, platform);
