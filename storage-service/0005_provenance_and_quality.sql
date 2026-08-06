-- Register immutable model identities, retain market-source provenance, and
-- finish the quality-fact contract introduced by 0003.

CREATE TABLE IF NOT EXISTS model_artifacts (
    model_kind          TEXT NOT NULL,
    model_hash          TEXT NOT NULL,
    first_pipeline_git_commit TEXT NOT NULL,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (model_kind, model_hash),
    CHECK (model_kind IN ('topic', 'sentiment')),
    CHECK (
        model_hash IN ('legacy', 'unknown')
        OR model_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS model_artifact_aliases (
    model_kind      TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    model_hash      TEXT NOT NULL,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (model_kind, model_version, model_hash),
    FOREIGN KEY (model_kind, model_hash)
        REFERENCES model_artifacts(model_kind, model_hash)
);

CREATE INDEX IF NOT EXISTS model_artifact_aliases_lookup_idx
    ON model_artifact_aliases (model_kind, model_version, last_seen_at DESC);

CREATE OR REPLACE FUNCTION register_post_model_artifacts()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO model_artifacts (
        model_kind, model_hash, first_pipeline_git_commit
    )
    VALUES
        ('topic', NEW.topic_model_hash, NEW.pipeline_git_commit),
        ('sentiment', NEW.sentiment_model_hash, NEW.pipeline_git_commit)
    ON CONFLICT (model_kind, model_hash) DO UPDATE SET
        last_seen_at = NOW();

    INSERT INTO model_artifact_aliases (
        model_kind, model_version, model_hash
    )
    VALUES
        ('topic', NEW.topic_model_version, NEW.topic_model_hash),
        ('sentiment', NEW.sentiment_model_version, NEW.sentiment_model_hash)
    ON CONFLICT (model_kind, model_version, model_hash) DO UPDATE SET
        last_seen_at = NOW();

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trigger_register_post_model_artifacts ON posts;
CREATE TRIGGER trigger_register_post_model_artifacts
BEFORE INSERT ON posts
FOR EACH ROW
EXECUTE FUNCTION register_post_model_artifacts();

INSERT INTO model_artifacts (
    model_kind, model_hash, first_pipeline_git_commit,
    first_seen_at, last_seen_at
)
SELECT
    'topic', topic_model_hash, MIN(pipeline_git_commit),
    MIN(ingested_at), MAX(ingested_at)
FROM posts
GROUP BY topic_model_hash
UNION ALL
SELECT
    'sentiment', sentiment_model_hash, MIN(pipeline_git_commit),
    MIN(ingested_at), MAX(ingested_at)
FROM posts
GROUP BY sentiment_model_hash
ON CONFLICT (model_kind, model_hash) DO NOTHING;

INSERT INTO model_artifact_aliases (
    model_kind, model_version, model_hash,
    first_seen_at, last_seen_at
)
SELECT
    'topic', topic_model_version, topic_model_hash,
    MIN(ingested_at), MAX(ingested_at)
FROM posts
GROUP BY topic_model_version, topic_model_hash
UNION ALL
SELECT
    'sentiment', sentiment_model_version, sentiment_model_hash,
    MIN(ingested_at), MAX(ingested_at)
FROM posts
GROUP BY sentiment_model_version, sentiment_model_hash
ON CONFLICT (model_kind, model_version, model_hash) DO NOTHING;

ALTER TABLE stock_quotes
    ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE market_hourly_facts
    ADD COLUMN IF NOT EXISTS providers TEXT[] NOT NULL DEFAULT ARRAY['legacy']::TEXT[];

ALTER TABLE market_daily_facts
    ADD COLUMN IF NOT EXISTS providers TEXT[] NOT NULL DEFAULT ARRAY['legacy']::TEXT[];

ALTER TABLE hourly_pipeline_quality_facts
    ADD COLUMN IF NOT EXISTS pipeline_version TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE hourly_pipeline_quality_facts
    DROP CONSTRAINT IF EXISTS hourly_pipeline_quality_facts_pkey;

ALTER TABLE hourly_pipeline_quality_facts
    ADD CONSTRAINT hourly_pipeline_quality_facts_pkey PRIMARY KEY (
        bucket_hour, pipeline_stage, platform, reason, pipeline_version
    );

COMMENT ON TABLE model_artifacts IS
    'Immutable model artifact identities. Human aliases live in model_artifact_aliases and may map to more than one hash over time.';
COMMENT ON COLUMN stock_quotes.provider IS
    'Provider observed for this quote; legacy denotes rows predating provenance capture.';
