-- Provenance, leakage-safe analytical facts, and archive lifecycle metadata.

ALTER TABLE posts
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS cleaned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS topic_scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS sentiment_scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS engagement_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS source_schema_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS pipeline_git_commit TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS topic_model_version TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS topic_model_hash TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS sentiment_model_version TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS sentiment_model_hash TEXT NOT NULL DEFAULT 'legacy';

-- Topic 0 is a real topic. -1 means the model's General/Outlier class, so -2 is
-- reserved for historical or otherwise unassigned topics.
UPDATE posts SET topic_id = -2 WHERE topic_id IS NULL;
ALTER TABLE posts ALTER COLUMN topic_id SET DEFAULT -2;
ALTER TABLE posts ALTER COLUMN topic_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS posts_retention_idx ON posts (timestamp, post_pk);
CREATE INDEX IF NOT EXISTS posts_model_identity_idx
    ON posts (sentiment_model_hash, topic_model_hash, timestamp DESC);

CREATE TABLE IF NOT EXISTS hourly_sentiment_facts (
    bucket_hour                 TIMESTAMPTZ NOT NULL,
    symbol                      TEXT NOT NULL,
    platform                    TEXT NOT NULL,
    topic_id                    INTEGER NOT NULL DEFAULT -2,
    topic_model_hash            TEXT NOT NULL,
    sentiment_model_hash        TEXT NOT NULL,
    agg_version                 SMALLINT NOT NULL DEFAULT 1,
    topic_model_version         TEXT NOT NULL,
    sentiment_model_version     TEXT NOT NULL,
    post_count                  BIGINT NOT NULL DEFAULT 0,
    positive_count              BIGINT NOT NULL DEFAULT 0,
    neutral_count               BIGINT NOT NULL DEFAULT 0,
    negative_count              BIGINT NOT NULL DEFAULT 0,
    positive_probability_sum    DOUBLE PRECISION NOT NULL DEFAULT 0,
    neutral_probability_sum     DOUBLE PRECISION NOT NULL DEFAULT 0,
    negative_probability_sum    DOUBLE PRECISION NOT NULL DEFAULT 0,
    positive_probability_sq_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    neutral_probability_sq_sum  DOUBLE PRECISION NOT NULL DEFAULT 0,
    negative_probability_sq_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    signal_sum                  DOUBLE PRECISION NOT NULL DEFAULT 0,
    signal_sq_sum               DOUBLE PRECISION NOT NULL DEFAULT 0,
    engagement_sum              BIGINT NOT NULL DEFAULT 0,
    weight_sum                  DOUBLE PRECISION NOT NULL DEFAULT 0,
    positive_weight_sum         DOUBLE PRECISION NOT NULL DEFAULT 0,
    neutral_weight_sum          DOUBLE PRECISION NOT NULL DEFAULT 0,
    negative_weight_sum         DOUBLE PRECISION NOT NULL DEFAULT 0,
    weighted_signal_sum         DOUBLE PRECISION NOT NULL DEFAULT 0,
    weighted_signal_sq_sum      DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_event_at              TIMESTAMPTZ NOT NULL,
    last_event_at               TIMESTAMPTZ NOT NULL,
    first_ingested_at           TIMESTAMPTZ NOT NULL,
    last_ingested_at            TIMESTAMPTZ NOT NULL,
    first_scored_at             TIMESTAMPTZ NOT NULL,
    last_scored_at              TIMESTAMPTZ NOT NULL,
    input_watermark             TIMESTAMPTZ NOT NULL,
    computed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        bucket_hour, symbol, platform, topic_id,
        topic_model_hash, sentiment_model_hash, agg_version
    ),
    CHECK (post_count >= 0),
    CHECK (positive_count + neutral_count + negative_count = post_count),
    CHECK (first_event_at <= last_event_at),
    CHECK (first_ingested_at <= last_ingested_at),
    CHECK (first_scored_at <= last_scored_at)
) PARTITION BY RANGE (bucket_hour);

CREATE INDEX IF NOT EXISTS hourly_sentiment_facts_lookup_idx
    ON hourly_sentiment_facts
    (symbol, bucket_hour DESC, platform, topic_id);

CREATE TABLE IF NOT EXISTS hourly_sentiment_fact_snapshots (
    maintenance_run_id          UUID NOT NULL,
    as_of                       TIMESTAMPTZ NOT NULL,
    bucket_hour                 TIMESTAMPTZ NOT NULL,
    symbol                      TEXT NOT NULL,
    platform                    TEXT NOT NULL,
    topic_id                    INTEGER NOT NULL,
    topic_model_hash            TEXT NOT NULL,
    sentiment_model_hash        TEXT NOT NULL,
    agg_version                 SMALLINT NOT NULL,
    topic_model_version         TEXT NOT NULL,
    sentiment_model_version     TEXT NOT NULL,
    post_count                  BIGINT NOT NULL,
    positive_count              BIGINT NOT NULL,
    neutral_count               BIGINT NOT NULL,
    negative_count              BIGINT NOT NULL,
    positive_probability_sum    DOUBLE PRECISION NOT NULL,
    neutral_probability_sum     DOUBLE PRECISION NOT NULL,
    negative_probability_sum    DOUBLE PRECISION NOT NULL,
    positive_probability_sq_sum DOUBLE PRECISION NOT NULL,
    neutral_probability_sq_sum  DOUBLE PRECISION NOT NULL,
    negative_probability_sq_sum DOUBLE PRECISION NOT NULL,
    signal_sum                  DOUBLE PRECISION NOT NULL,
    signal_sq_sum               DOUBLE PRECISION NOT NULL,
    engagement_sum              BIGINT NOT NULL,
    weight_sum                  DOUBLE PRECISION NOT NULL,
    positive_weight_sum         DOUBLE PRECISION NOT NULL,
    neutral_weight_sum          DOUBLE PRECISION NOT NULL,
    negative_weight_sum         DOUBLE PRECISION NOT NULL,
    weighted_signal_sum         DOUBLE PRECISION NOT NULL,
    weighted_signal_sq_sum      DOUBLE PRECISION NOT NULL,
    first_event_at              TIMESTAMPTZ NOT NULL,
    last_event_at               TIMESTAMPTZ NOT NULL,
    first_ingested_at           TIMESTAMPTZ NOT NULL,
    last_ingested_at            TIMESTAMPTZ NOT NULL,
    first_scored_at             TIMESTAMPTZ NOT NULL,
    last_scored_at              TIMESTAMPTZ NOT NULL,
    input_watermark             TIMESTAMPTZ NOT NULL,
    computed_at                 TIMESTAMPTZ NOT NULL,
    finalized_at                TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (
        bucket_hour, symbol, platform, topic_id, topic_model_hash,
        sentiment_model_hash, agg_version, maintenance_run_id
    )
) PARTITION BY RANGE (bucket_hour);

CREATE INDEX IF NOT EXISTS hourly_sentiment_snapshots_asof_idx
    ON hourly_sentiment_fact_snapshots (symbol, as_of DESC, bucket_hour DESC);

CREATE TABLE IF NOT EXISTS hourly_pipeline_quality_facts (
    bucket_hour         TIMESTAMPTZ NOT NULL,
    pipeline_stage      TEXT NOT NULL,
    platform            TEXT NOT NULL,
    reason              TEXT NOT NULL,
    count               BIGINT NOT NULL DEFAULT 0,
    first_observed_at   TIMESTAMPTZ NOT NULL,
    last_observed_at    TIMESTAMPTZ NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bucket_hour, pipeline_stage, platform, reason),
    CHECK (count >= 0)
) PARTITION BY RANGE (bucket_hour);

CREATE OR REPLACE FUNCTION ensure_hourly_analytical_partitions(
    from_timestamp TIMESTAMPTZ,
    through_timestamp TIMESTAMPTZ
) RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    partition_start DATE;
    partition_end DATE;
    suffix TEXT;
    created_count INTEGER := 0;
    parent_table TEXT;
BEGIN
    IF from_timestamp IS NULL OR through_timestamp IS NULL THEN
        RETURN 0;
    END IF;
    IF from_timestamp > through_timestamp THEN
        RAISE EXCEPTION 'partition start % is after end %',
            from_timestamp, through_timestamp;
    END IF;

    partition_start := date_trunc('month', from_timestamp)::DATE;
    WHILE partition_start <= date_trunc('month', through_timestamp)::DATE LOOP
        partition_end := (partition_start + INTERVAL '1 month')::DATE;
        suffix := to_char(partition_start, 'YYYYMM');
        FOREACH parent_table IN ARRAY ARRAY[
            'hourly_sentiment_facts',
            'hourly_sentiment_fact_snapshots',
            'hourly_pipeline_quality_facts'
        ] LOOP
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I '
                'FOR VALUES FROM (%L) TO (%L)',
                parent_table || '_' || suffix,
                parent_table,
                partition_start,
                partition_end
            );
            created_count := created_count + 1;
        END LOOP;
        partition_start := partition_end;
    END LOOP;
    RETURN created_count;
END;
$$;

SELECT ensure_hourly_analytical_partitions(
    COALESCE((SELECT MIN(timestamp) FROM posts), NOW()),
    NOW() + INTERVAL '3 months'
);

CREATE TABLE IF NOT EXISTS analytical_signal_snapshots (
    snapshot_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol                  TEXT NOT NULL,
    signal_name             TEXT NOT NULL,
    algorithm               TEXT NOT NULL,
    algorithm_version       TEXT NOT NULL,
    parameters              JSONB NOT NULL,
    input_window_start      TIMESTAMPTZ NOT NULL,
    input_window_end        TIMESTAMPTZ NOT NULL,
    input_watermark         TIMESTAMPTZ NOT NULL,
    as_of                   TIMESTAMPTZ NOT NULL,
    topic_model_hash        TEXT,
    sentiment_model_hash    TEXT,
    pipeline_git_commit     TEXT NOT NULL,
    result                  JSONB NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (input_window_start < input_window_end),
    CHECK (input_watermark <= as_of)
);

CREATE INDEX IF NOT EXISTS analytical_signal_snapshots_lookup_idx
    ON analytical_signal_snapshots
    (symbol, signal_name, algorithm_version, as_of DESC);

CREATE TABLE IF NOT EXISTS market_hourly_facts (
    symbol              TEXT NOT NULL,
    bucket_hour         TIMESTAMPTZ NOT NULL,
    open_price          DOUBLE PRECISION NOT NULL,
    high_price          DOUBLE PRECISION NOT NULL,
    low_price           DOUBLE PRECISION NOT NULL,
    close_price         DOUBLE PRECISION NOT NULL,
    last_volume         BIGINT NOT NULL,
    observation_count   BIGINT NOT NULL,
    had_regular_session BOOLEAN NOT NULL,
    first_observed_at   TIMESTAMPTZ NOT NULL,
    last_observed_at    TIMESTAMPTZ NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, bucket_hour),
    CHECK (observation_count > 0),
    CHECK (low_price <= high_price)
);

CREATE TABLE IF NOT EXISTS market_daily_facts (
    symbol              TEXT NOT NULL,
    bucket_day          DATE NOT NULL,
    open_price          DOUBLE PRECISION NOT NULL,
    high_price          DOUBLE PRECISION NOT NULL,
    low_price           DOUBLE PRECISION NOT NULL,
    close_price         DOUBLE PRECISION NOT NULL,
    last_volume         BIGINT NOT NULL,
    observation_count   BIGINT NOT NULL,
    had_regular_session BOOLEAN NOT NULL,
    first_observed_at   TIMESTAMPTZ NOT NULL,
    last_observed_at    TIMESTAMPTZ NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, bucket_day),
    CHECK (observation_count > 0),
    CHECK (low_price <= high_price)
);

CREATE TABLE IF NOT EXISTS raw_post_archive_staging (
    platform                    TEXT NOT NULL,
    source_post_id              TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    cohort                      TEXT NOT NULL,
    text                        TEXT NOT NULL,
    event_at                    TIMESTAMPTZ NOT NULL,
    ingested_at                 TIMESTAMPTZ NOT NULL,
    engagement                  INTEGER NOT NULL,
    engagement_observed_at      TIMESTAMPTZ NOT NULL,
    sentiment                   TEXT NOT NULL,
    scores                      JSONB NOT NULL,
    topic_id                    INTEGER NOT NULL,
    topic_label                 TEXT,
    topic_model_version         TEXT NOT NULL,
    topic_model_hash            TEXT NOT NULL,
    sentiment_model_version     TEXT NOT NULL,
    sentiment_model_hash        TEXT NOT NULL,
    source_schema_version       INTEGER NOT NULL,
    pipeline_git_commit         TEXT NOT NULL,
    inclusion_probability       DOUBLE PRECISION,
    selection_reasons           TEXT[] NOT NULL,
    staged_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at                 TIMESTAMPTZ,
    manifest_id                 UUID,
    PRIMARY KEY (platform, source_post_id, symbol, cohort),
    CHECK (cohort IN ('probability', 'challenge')),
    CHECK (
        (cohort = 'probability' AND inclusion_probability > 0
            AND inclusion_probability <= 1)
        OR (cohort = 'challenge' AND inclusion_probability IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS raw_post_archive_pending_idx
    ON raw_post_archive_staging (staged_at)
    WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS archive_manifests (
    manifest_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset             TEXT NOT NULL,
    object_key          TEXT NOT NULL UNIQUE,
    schema_version      INTEGER NOT NULL,
    partition_start     TIMESTAMPTZ NOT NULL,
    partition_end       TIMESTAMPTZ NOT NULL,
    row_count           BIGINT NOT NULL,
    min_event_at        TIMESTAMPTZ,
    max_event_at        TIMESTAMPTZ,
    sha256              TEXT NOT NULL,
    byte_size           BIGINT NOT NULL,
    object_etag         TEXT,
    object_version_id   TEXT,
    verified_at         TIMESTAMPTZ,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (partition_start < partition_end),
    CHECK (row_count >= 0),
    CHECK (byte_size >= 0),
    CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

ALTER TABLE raw_post_archive_staging
    ADD CONSTRAINT raw_post_archive_manifest_fk
    FOREIGN KEY (manifest_id) REFERENCES archive_manifests(manifest_id);

COMMENT ON TABLE hourly_sentiment_agg IS
    'Legacy unfiltered (symbol, hour) aggregate. It lacks platform, topic, and model provenance; never use it to answer filtered historical queries.';
COMMENT ON TABLE hourly_sentiment_facts IS
    'Canonical additive hourly facts. Derive nonlinear metrics only after SUM rollups.';
COMMENT ON TABLE raw_post_archive_staging IS
    'Opt-in cleaned-text archive staging. Empty platform allowlist means no text is retained here.';
