use anyhow::Result;
use chrono::{DateTime, Utc};
use social_sentiment_core::schemas::ScoredPost;
use sqlx::{postgres::PgPoolOptions, PgPool, Row};
use std::collections::HashSet;
use uuid::Uuid;

pub const POST_RETENTION_ADVISORY_LOCK_KEY: i64 = 4815162343;
pub const QUOTE_RETENTION_ADVISORY_LOCK_KEY: i64 = 4815162344;

pub const INSERT_POST_SQL: &str = r#"
    INSERT INTO posts (
        id, symbol, platform, text, timestamp, sentiment, scores,
        topic_id, topic_label, engagement, ingested_at, cleaned_at,
        topic_scored_at, sentiment_scored_at, engagement_observed_at,
        source_schema_version, pipeline_git_commit, topic_model_version,
        topic_model_hash, sentiment_model_version, sentiment_model_hash
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
        $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
    )
    ON CONFLICT (platform, id, symbol) DO NOTHING
    RETURNING id
"#;

pub const UPSERT_PIPELINE_QUALITY_SQL: &str = r#"
    INSERT INTO hourly_pipeline_quality_facts (
        bucket_hour, pipeline_stage, platform, reason, pipeline_version,
        count, first_observed_at, last_observed_at
    )
    VALUES (
        date_trunc('hour', NOW()), 'storage', $1,
        'duplicate_or_conflict', 'storage-insert-v1', $2, NOW(), NOW()
    )
    ON CONFLICT (
        bucket_hour, pipeline_stage, platform, reason, pipeline_version
    ) DO UPDATE SET
        count = hourly_pipeline_quality_facts.count + EXCLUDED.count,
        last_observed_at = EXCLUDED.last_observed_at,
        computed_at = NOW()
"#;

pub const ROLLUP_AND_PRUNE_POSTS_SQL: &str = r#"
    WITH maintenance_run AS MATERIALIZED (
        SELECT
            pg_advisory_xact_lock($1),
            $2::uuid AS run_id,
            NOW() AS as_of
    ),
    retention_candidates AS MATERIALIZED (
        SELECT
            p.*,
            probabilities.positive_probability,
            probabilities.neutral_probability,
            probabilities.negative_probability,
            probabilities.positive_probability
                - probabilities.negative_probability AS signal,
            LN(GREATEST(p.engagement, 0) + 1.0) AS weight
        FROM posts AS p
        CROSS JOIN LATERAL (
            SELECT
                COALESCE(
                    (p.scores ->> 'positive')::DOUBLE PRECISION,
                    CASE WHEN p.sentiment = 'positive' THEN 1.0 ELSE 0.0 END
                ) AS positive_probability,
                COALESCE(
                    (p.scores ->> 'neutral')::DOUBLE PRECISION,
                    CASE WHEN p.sentiment = 'neutral' THEN 1.0 ELSE 0.0 END
                ) AS neutral_probability,
                COALESCE(
                    (p.scores ->> 'negative')::DOUBLE PRECISION,
                    CASE WHEN p.sentiment = 'negative' THEN 1.0 ELSE 0.0 END
                ) AS negative_probability
        ) AS probabilities
        WHERE p.timestamp < $3
          AND EXISTS (SELECT 1 FROM maintenance_run)
    ),
    archived_candidates AS (
        INSERT INTO raw_post_archive_staging (
            platform, source_post_id, symbol, cohort, text, event_at,
            ingested_at, engagement, engagement_observed_at, sentiment,
            scores, topic_id, topic_label, topic_model_version,
            topic_model_hash, sentiment_model_version, sentiment_model_hash,
            source_schema_version, pipeline_git_commit,
            inclusion_probability, selection_reasons,
            sampling_policy_version, stratum
        )
        SELECT
            c.platform, c.id, c.symbol, 'probability', c.text, c.timestamp,
            c.ingested_at, c.engagement, c.engagement_observed_at,
            c.sentiment, c.scores, c.topic_id, c.topic_label,
            c.topic_model_version, c.topic_model_hash,
            c.sentiment_model_version, c.sentiment_model_hash,
            c.source_schema_version, c.pipeline_git_commit,
            $4::DOUBLE PRECISION,
            ARRAY['deterministic_uniform_sample']::TEXT[],
            'sample-v1',
            CONCAT_WS(
                ':', c.platform, c.topic_model_hash, c.topic_id,
                c.sentiment_model_hash, c.sentiment
            )
        FROM retention_candidates AS c
        WHERE c.platform = ANY($5::TEXT[])
          AND (
              ('x' || SUBSTR(
                  MD5(CONCAT_WS(
                      CHR(31), 'sample-v1', c.platform, c.id, c.symbol
                  )),
                  1,
                  8
              ))::BIT(32)::BIGINT / 4294967296.0
          ) < $6::DOUBLE PRECISION

        UNION ALL

        SELECT
            c.platform, c.id, c.symbol, 'challenge', c.text, c.timestamp,
            c.ingested_at, c.engagement, c.engagement_observed_at,
            c.sentiment, c.scores, c.topic_id, c.topic_label,
            c.topic_model_version, c.topic_model_hash,
            c.sentiment_model_version, c.sentiment_model_hash,
            c.source_schema_version, c.pipeline_git_commit,
            NULL::DOUBLE PRECISION,
            ARRAY_REMOVE(
                ARRAY[
                    CASE WHEN c.engagement >= $7
                        THEN 'high_engagement' END,
                    CASE WHEN ABS(c.signal) >= $8
                        THEN 'extreme_sentiment' END
                ],
                NULL
            )::TEXT[],
            'sample-v1',
            CONCAT_WS(
                ':', c.platform, c.topic_model_hash, c.topic_id,
                c.sentiment_model_hash, c.sentiment
            )
        FROM retention_candidates AS c
        WHERE c.platform = ANY($9::TEXT[])
          AND (c.engagement >= $10 OR ABS(c.signal) >= $11)
        ON CONFLICT (platform, source_post_id, symbol, cohort) DO NOTHING
        RETURNING 1
    ),
    deleted_posts AS (
        DELETE FROM posts AS p
        USING retention_candidates AS c
        WHERE p.post_pk = c.post_pk
        RETURNING
            p.symbol, p.platform, p.timestamp, p.sentiment, p.engagement,
            p.ingested_at, p.sentiment_scored_at, p.topic_id, p.topic_label,
            p.topic_model_version, p.topic_model_hash,
            p.sentiment_model_version, p.sentiment_model_hash,
            c.positive_probability, c.neutral_probability,
            c.negative_probability, c.signal, c.weight
    ),
    aggregated_posts AS (
        SELECT
            symbol,
            date_trunc('hour', timestamp, 'UTC') AS bucket_hour,
            platform,
            topic_id,
            topic_model_hash,
            sentiment_model_hash,
            1::SMALLINT AS agg_version,
            MAX(topic_model_version) AS topic_model_version,
            MAX(sentiment_model_version) AS sentiment_model_version,
            MAX(COALESCE(topic_label, 'Unassigned')) AS topic_label,
            COUNT(*) AS post_count,
            COUNT(*) FILTER (WHERE sentiment = 'positive') AS positive_count,
            COUNT(*) FILTER (WHERE sentiment = 'neutral') AS neutral_count,
            COUNT(*) FILTER (WHERE sentiment = 'negative') AS negative_count,
            SUM(positive_probability) AS positive_probability_sum,
            SUM(neutral_probability) AS neutral_probability_sum,
            SUM(negative_probability) AS negative_probability_sum,
            SUM(positive_probability * positive_probability)
                AS positive_probability_sq_sum,
            SUM(neutral_probability * neutral_probability)
                AS neutral_probability_sq_sum,
            SUM(negative_probability * negative_probability)
                AS negative_probability_sq_sum,
            SUM(signal) AS signal_sum,
            SUM(signal * signal) AS signal_sq_sum,
            SUM(GREATEST(engagement, 0)) AS engagement_sum,
            SUM(weight) AS weight_sum,
            SUM(weight) FILTER (WHERE sentiment = 'positive')
                AS positive_weight_sum,
            SUM(weight) FILTER (WHERE sentiment = 'neutral')
                AS neutral_weight_sum,
            SUM(weight) FILTER (WHERE sentiment = 'negative')
                AS negative_weight_sum,
            SUM(weight * signal) AS weighted_signal_sum,
            SUM(weight * signal * signal) AS weighted_signal_sq_sum,
            MIN(timestamp) AS first_event_at,
            MAX(timestamp) AS last_event_at,
            MIN(ingested_at) AS first_ingested_at,
            MAX(ingested_at) AS last_ingested_at,
            MIN(sentiment_scored_at) AS first_scored_at,
            MAX(sentiment_scored_at) AS last_scored_at,
            MAX(ingested_at) AS input_watermark
        FROM deleted_posts
        GROUP BY
            symbol,
            date_trunc('hour', timestamp, 'UTC'),
            platform,
            topic_id,
            topic_model_hash,
            sentiment_model_hash
    ),
    legacy_aggregates AS (
        SELECT
            symbol,
            bucket_hour,
            SUM(positive_count) AS positive_count,
            SUM(neutral_count) AS neutral_count,
            SUM(negative_count) AS negative_count,
            COALESCE(SUM(positive_weight_sum), 0) AS positive_weighted,
            COALESCE(SUM(negative_weight_sum), 0) AS negative_weighted,
            COALESCE(SUM(neutral_weight_sum), 0) AS neutral_weighted,
            COALESCE(SUM(weight_sum), 0) AS total_weighted
        FROM aggregated_posts
        GROUP BY symbol, bucket_hour
    ),
    legacy_upserts AS (
        INSERT INTO hourly_sentiment_agg (
            symbol, bucket_hour,
            positive_count, neutral_count, negative_count,
            positive_weighted, negative_weighted, neutral_weighted,
            total_weighted, sentiment_index
        )
        SELECT
            symbol,
            bucket_hour,
            positive_count,
            neutral_count,
            negative_count,
            positive_weighted,
            negative_weighted,
            neutral_weighted,
            total_weighted,
            CASE
                WHEN total_weighted > 0
                THEN (positive_weighted - negative_weighted) / total_weighted
                ELSE 0.0
            END
        FROM legacy_aggregates
        ORDER BY symbol, bucket_hour
        ON CONFLICT (symbol, bucket_hour) DO UPDATE SET
            positive_count = hourly_sentiment_agg.positive_count
                + EXCLUDED.positive_count,
            neutral_count = hourly_sentiment_agg.neutral_count
                + EXCLUDED.neutral_count,
            negative_count = hourly_sentiment_agg.negative_count
                + EXCLUDED.negative_count,
            positive_weighted = hourly_sentiment_agg.positive_weighted
                + EXCLUDED.positive_weighted,
            negative_weighted = hourly_sentiment_agg.negative_weighted
                + EXCLUDED.negative_weighted,
            neutral_weighted = hourly_sentiment_agg.neutral_weighted
                + EXCLUDED.neutral_weighted,
            total_weighted = hourly_sentiment_agg.total_weighted
                + EXCLUDED.total_weighted,
            sentiment_index = CASE
                WHEN hourly_sentiment_agg.total_weighted
                     + EXCLUDED.total_weighted > 0
                THEN (
                    hourly_sentiment_agg.positive_weighted
                    + EXCLUDED.positive_weighted
                    - hourly_sentiment_agg.negative_weighted
                    - EXCLUDED.negative_weighted
                ) / (
                    hourly_sentiment_agg.total_weighted
                    + EXCLUDED.total_weighted
                )
                ELSE 0.0
            END
        RETURNING 1
    ),
    fact_upserts AS (
        INSERT INTO hourly_sentiment_facts (
            bucket_hour, symbol, platform, topic_id, topic_model_hash,
            sentiment_model_hash, agg_version, topic_model_version,
            sentiment_model_version, post_count, positive_count,
            neutral_count, negative_count, positive_probability_sum,
            neutral_probability_sum, negative_probability_sum,
            positive_probability_sq_sum, neutral_probability_sq_sum,
            negative_probability_sq_sum, signal_sum, signal_sq_sum,
            engagement_sum, weight_sum, positive_weight_sum,
            neutral_weight_sum, negative_weight_sum, weighted_signal_sum,
            weighted_signal_sq_sum, first_event_at, last_event_at,
            first_ingested_at, last_ingested_at, first_scored_at,
            last_scored_at, input_watermark, computed_at, finalized_at,
            topic_label
        )
        SELECT
            a.bucket_hour, a.symbol, a.platform, a.topic_id,
            a.topic_model_hash, a.sentiment_model_hash, a.agg_version,
            a.topic_model_version, a.sentiment_model_version, a.post_count,
            a.positive_count, a.neutral_count, a.negative_count,
            a.positive_probability_sum, a.neutral_probability_sum,
            a.negative_probability_sum, a.positive_probability_sq_sum,
            a.neutral_probability_sq_sum, a.negative_probability_sq_sum,
            a.signal_sum, a.signal_sq_sum, a.engagement_sum, a.weight_sum,
            COALESCE(a.positive_weight_sum, 0),
            COALESCE(a.neutral_weight_sum, 0),
            COALESCE(a.negative_weight_sum, 0),
            a.weighted_signal_sum, a.weighted_signal_sq_sum,
            a.first_event_at, a.last_event_at, a.first_ingested_at,
            a.last_ingested_at, a.first_scored_at, a.last_scored_at,
            a.input_watermark, m.as_of, m.as_of, a.topic_label
        FROM aggregated_posts AS a
        CROSS JOIN maintenance_run AS m
        ORDER BY
            a.bucket_hour, a.symbol, a.platform, a.topic_id,
            a.topic_model_hash, a.sentiment_model_hash
        ON CONFLICT (
            bucket_hour, symbol, platform, topic_id,
            topic_model_hash, sentiment_model_hash, agg_version
        ) DO UPDATE SET
            topic_model_version = EXCLUDED.topic_model_version,
            sentiment_model_version = EXCLUDED.sentiment_model_version,
            topic_label = EXCLUDED.topic_label,
            post_count = hourly_sentiment_facts.post_count
                + EXCLUDED.post_count,
            positive_count = hourly_sentiment_facts.positive_count
                + EXCLUDED.positive_count,
            neutral_count = hourly_sentiment_facts.neutral_count
                + EXCLUDED.neutral_count,
            negative_count = hourly_sentiment_facts.negative_count
                + EXCLUDED.negative_count,
            positive_probability_sum =
                hourly_sentiment_facts.positive_probability_sum
                + EXCLUDED.positive_probability_sum,
            neutral_probability_sum =
                hourly_sentiment_facts.neutral_probability_sum
                + EXCLUDED.neutral_probability_sum,
            negative_probability_sum =
                hourly_sentiment_facts.negative_probability_sum
                + EXCLUDED.negative_probability_sum,
            positive_probability_sq_sum =
                hourly_sentiment_facts.positive_probability_sq_sum
                + EXCLUDED.positive_probability_sq_sum,
            neutral_probability_sq_sum =
                hourly_sentiment_facts.neutral_probability_sq_sum
                + EXCLUDED.neutral_probability_sq_sum,
            negative_probability_sq_sum =
                hourly_sentiment_facts.negative_probability_sq_sum
                + EXCLUDED.negative_probability_sq_sum,
            signal_sum = hourly_sentiment_facts.signal_sum
                + EXCLUDED.signal_sum,
            signal_sq_sum = hourly_sentiment_facts.signal_sq_sum
                + EXCLUDED.signal_sq_sum,
            engagement_sum = hourly_sentiment_facts.engagement_sum
                + EXCLUDED.engagement_sum,
            weight_sum = hourly_sentiment_facts.weight_sum
                + EXCLUDED.weight_sum,
            positive_weight_sum = hourly_sentiment_facts.positive_weight_sum
                + EXCLUDED.positive_weight_sum,
            neutral_weight_sum = hourly_sentiment_facts.neutral_weight_sum
                + EXCLUDED.neutral_weight_sum,
            negative_weight_sum = hourly_sentiment_facts.negative_weight_sum
                + EXCLUDED.negative_weight_sum,
            weighted_signal_sum =
                hourly_sentiment_facts.weighted_signal_sum
                + EXCLUDED.weighted_signal_sum,
            weighted_signal_sq_sum =
                hourly_sentiment_facts.weighted_signal_sq_sum
                + EXCLUDED.weighted_signal_sq_sum,
            first_event_at = LEAST(
                hourly_sentiment_facts.first_event_at,
                EXCLUDED.first_event_at
            ),
            last_event_at = GREATEST(
                hourly_sentiment_facts.last_event_at,
                EXCLUDED.last_event_at
            ),
            first_ingested_at = LEAST(
                hourly_sentiment_facts.first_ingested_at,
                EXCLUDED.first_ingested_at
            ),
            last_ingested_at = GREATEST(
                hourly_sentiment_facts.last_ingested_at,
                EXCLUDED.last_ingested_at
            ),
            first_scored_at = LEAST(
                hourly_sentiment_facts.first_scored_at,
                EXCLUDED.first_scored_at
            ),
            last_scored_at = GREATEST(
                hourly_sentiment_facts.last_scored_at,
                EXCLUDED.last_scored_at
            ),
            input_watermark = GREATEST(
                hourly_sentiment_facts.input_watermark,
                EXCLUDED.input_watermark
            ),
            computed_at = EXCLUDED.computed_at,
            finalized_at = EXCLUDED.finalized_at
        RETURNING hourly_sentiment_facts.*
    ),
    fact_snapshots AS (
        INSERT INTO hourly_sentiment_fact_snapshots
        SELECT m.run_id, m.as_of, f.*
        FROM fact_upserts AS f
        CROSS JOIN maintenance_run AS m
        RETURNING 1
    )
    SELECT
        (SELECT COUNT(*) FROM legacy_upserts) AS rolled_up_buckets,
        (SELECT COUNT(*) FROM deleted_posts) AS pruned_posts,
        (SELECT COUNT(*) FROM fact_upserts) AS canonical_facts,
        (SELECT COUNT(*) FROM fact_snapshots) AS fact_snapshots,
        (SELECT COUNT(*) FROM archived_candidates) AS staged_samples
"#;

pub const ROLLUP_AND_PRUNE_QUOTES_SQL: &str = r#"
    WITH maintenance_lock AS MATERIALIZED (
        SELECT pg_advisory_xact_lock($1)
    ),
    deleted_quotes AS (
        DELETE FROM stock_quotes
        WHERE timestamp < $2
          AND EXISTS (SELECT 1 FROM maintenance_lock)
        RETURNING symbol, timestamp, price, volume, market_session, provider
    ),
    hourly AS (
        SELECT
            symbol,
            date_trunc('hour', timestamp, 'UTC') AS bucket_hour,
            (ARRAY_AGG(price ORDER BY timestamp))[1] AS open_price,
            MAX(price) AS high_price,
            MIN(price) AS low_price,
            (ARRAY_AGG(price ORDER BY timestamp DESC))[1] AS close_price,
            (ARRAY_AGG(volume ORDER BY timestamp DESC))[1] AS last_volume,
            ARRAY_AGG(DISTINCT provider ORDER BY provider) AS providers,
            COUNT(*) AS observation_count,
            BOOL_OR(market_session = 'regular') AS had_regular_session,
            MIN(timestamp) AS first_observed_at,
            MAX(timestamp) AS last_observed_at
        FROM deleted_quotes
        GROUP BY symbol, date_trunc('hour', timestamp, 'UTC')
    ),
    hourly_upserts AS (
        INSERT INTO market_hourly_facts (
            symbol, bucket_hour, open_price, high_price, low_price,
            close_price, last_volume, observation_count,
            had_regular_session, first_observed_at, last_observed_at, providers
        )
        SELECT
            symbol, bucket_hour, open_price, high_price, low_price,
            close_price, last_volume, observation_count,
            had_regular_session, first_observed_at, last_observed_at, providers
        FROM hourly
        ORDER BY symbol, bucket_hour
        ON CONFLICT (symbol, bucket_hour) DO UPDATE SET
            open_price = CASE
                WHEN EXCLUDED.first_observed_at
                    < market_hourly_facts.first_observed_at
                THEN EXCLUDED.open_price
                ELSE market_hourly_facts.open_price
            END,
            high_price = GREATEST(
                market_hourly_facts.high_price,
                EXCLUDED.high_price
            ),
            low_price = LEAST(
                market_hourly_facts.low_price,
                EXCLUDED.low_price
            ),
            close_price = CASE
                WHEN EXCLUDED.last_observed_at
                    >= market_hourly_facts.last_observed_at
                THEN EXCLUDED.close_price
                ELSE market_hourly_facts.close_price
            END,
            last_volume = CASE
                WHEN EXCLUDED.last_observed_at
                    >= market_hourly_facts.last_observed_at
                THEN EXCLUDED.last_volume
                ELSE market_hourly_facts.last_volume
            END,
            observation_count = market_hourly_facts.observation_count
                + EXCLUDED.observation_count,
            providers = ARRAY(
                SELECT DISTINCT provider
                FROM unnest(
                    market_hourly_facts.providers || EXCLUDED.providers
                ) AS provider
                ORDER BY provider
            ),
            had_regular_session = market_hourly_facts.had_regular_session
                OR EXCLUDED.had_regular_session,
            first_observed_at = LEAST(
                market_hourly_facts.first_observed_at,
                EXCLUDED.first_observed_at
            ),
            last_observed_at = GREATEST(
                market_hourly_facts.last_observed_at,
                EXCLUDED.last_observed_at
            ),
            computed_at = NOW()
        RETURNING 1
    ),
    daily AS (
        SELECT
            symbol,
            (timestamp AT TIME ZONE 'America/New_York')::DATE AS bucket_day,
            (ARRAY_AGG(price ORDER BY timestamp))[1] AS open_price,
            MAX(price) AS high_price,
            MIN(price) AS low_price,
            (ARRAY_AGG(price ORDER BY timestamp DESC))[1] AS close_price,
            (ARRAY_AGG(volume ORDER BY timestamp DESC))[1] AS last_volume,
            ARRAY_AGG(DISTINCT provider ORDER BY provider) AS providers,
            COUNT(*) AS observation_count,
            BOOL_OR(market_session = 'regular') AS had_regular_session,
            MIN(timestamp) AS first_observed_at,
            MAX(timestamp) AS last_observed_at
        FROM deleted_quotes
        GROUP BY
            symbol,
            (timestamp AT TIME ZONE 'America/New_York')::DATE
    ),
    daily_upserts AS (
        INSERT INTO market_daily_facts (
            symbol, bucket_day, open_price, high_price, low_price,
            close_price, last_volume, observation_count,
            had_regular_session, first_observed_at, last_observed_at, providers
        )
        SELECT
            symbol, bucket_day, open_price, high_price, low_price,
            close_price, last_volume, observation_count,
            had_regular_session, first_observed_at, last_observed_at, providers
        FROM daily
        ORDER BY symbol, bucket_day
        ON CONFLICT (symbol, bucket_day) DO UPDATE SET
            open_price = CASE
                WHEN EXCLUDED.first_observed_at
                    < market_daily_facts.first_observed_at
                THEN EXCLUDED.open_price
                ELSE market_daily_facts.open_price
            END,
            high_price = GREATEST(
                market_daily_facts.high_price,
                EXCLUDED.high_price
            ),
            low_price = LEAST(
                market_daily_facts.low_price,
                EXCLUDED.low_price
            ),
            close_price = CASE
                WHEN EXCLUDED.last_observed_at
                    >= market_daily_facts.last_observed_at
                THEN EXCLUDED.close_price
                ELSE market_daily_facts.close_price
            END,
            last_volume = CASE
                WHEN EXCLUDED.last_observed_at
                    >= market_daily_facts.last_observed_at
                THEN EXCLUDED.last_volume
                ELSE market_daily_facts.last_volume
            END,
            observation_count = market_daily_facts.observation_count
                + EXCLUDED.observation_count,
            providers = ARRAY(
                SELECT DISTINCT provider
                FROM unnest(
                    market_daily_facts.providers || EXCLUDED.providers
                ) AS provider
                ORDER BY provider
            ),
            had_regular_session = market_daily_facts.had_regular_session
                OR EXCLUDED.had_regular_session,
            first_observed_at = LEAST(
                market_daily_facts.first_observed_at,
                EXCLUDED.first_observed_at
            ),
            last_observed_at = GREATEST(
                market_daily_facts.last_observed_at,
                EXCLUDED.last_observed_at
            ),
            computed_at = NOW()
        RETURNING 1
    )
    SELECT
        (SELECT COUNT(*) FROM hourly_upserts) AS hourly_facts,
        (SELECT COUNT(*) FROM daily_upserts) AS daily_facts,
        (SELECT COUNT(*) FROM deleted_quotes) AS pruned_quotes
"#;

pub struct DatabaseService {
    pool: PgPool,
}

impl DatabaseService {
    pub async fn connect(dsn: &str) -> Result<Self> {
        let pool = PgPoolOptions::new()
            .min_connections(2)
            .max_connections(10)
            .connect(dsn)
            .await?;
        Ok(Self { pool })
    }

    pub fn batch_platform(posts: &[ScoredPost]) -> String {
        let platforms: HashSet<&str> = posts.iter().map(|p| p.clean.platform.as_str()).collect();
        if platforms.len() == 1 {
            platforms.into_iter().next().unwrap().to_string()
        } else {
            "mixed".to_string()
        }
    }

    pub async fn insert_scored_batch(&self, posts: &[ScoredPost]) -> Result<u64> {
        if posts.is_empty() {
            return Ok(0);
        }

        let mut tx = self.pool.begin().await?;
        let mut inserted_count = 0u64;

        for post in posts {
            let text_clean = post.clean.text.replace('\x00', "");
            let topic_label_clean = post
                .clean
                .topic_label
                .as_ref()
                .map(|l| l.replace('\x00', ""));
            let topic_id = post.clean.topic_id.unwrap_or(-2);
            let scores_json = serde_json::to_value(&post.scores)?;

            let res = sqlx::query(INSERT_POST_SQL)
                .bind(&post.clean.id)
                .bind(&post.clean.symbol)
                .bind(&post.clean.platform)
                .bind(text_clean)
                .bind(post.clean.timestamp)
                .bind(&post.sentiment)
                .bind(scores_json)
                .bind(topic_id)
                .bind(topic_label_clean)
                .bind(post.clean.engagement)
                .bind(post.clean.ingested_at)
                .bind(post.clean.cleaned_at)
                .bind(post.clean.topic_scored_at)
                .bind(post.sentiment_scored_at)
                .bind(post.clean.engagement_observed_at)
                .bind(post.clean.source_schema_version)
                .bind(&post.clean.pipeline_git_commit)
                .bind(&post.clean.topic_model_version)
                .bind(&post.clean.topic_model_hash)
                .bind(&post.sentiment_model_version)
                .bind(&post.sentiment_model_hash)
                .execute(&mut *tx)
                .await?;

            inserted_count += res.rows_affected();
        }

        let duplicate_count = (posts.len() as u64).saturating_sub(inserted_count);
        if duplicate_count > 0 {
            let platform = Self::batch_platform(posts);
            sqlx::query(UPSERT_PIPELINE_QUALITY_SQL)
                .bind(platform)
                .bind(duplicate_count as i64)
                .execute(&mut *tx)
                .await?;
        }

        tx.commit().await?;
        Ok(inserted_count)
    }

    pub async fn rollup_and_prune_posts(
        &self,
        cutoff_ts: DateTime<Utc>,
        archive_platforms: &[String],
        sample_rate: f64,
        challenge_engagement: i32,
        challenge_abs_signal: f64,
    ) -> Result<(i64, i64)> {
        let maintenance_run_id = Uuid::new_v4();

        // Preflight partition creation check
        sqlx::query(
            r#"
            SELECT
                pg_advisory_xact_lock($1),
                ensure_hourly_analytical_partitions(
                    COALESCE(
                        (SELECT MIN(timestamp) FROM posts WHERE timestamp < $2),
                        $3
                    ),
                    NOW() + INTERVAL '3 months'
                )
            "#,
        )
        .bind(POST_RETENTION_ADVISORY_LOCK_KEY)
        .bind(cutoff_ts)
        .bind(cutoff_ts)
        .execute(&self.pool)
        .await?;

        let row = sqlx::query(ROLLUP_AND_PRUNE_POSTS_SQL)
            .bind(POST_RETENTION_ADVISORY_LOCK_KEY)
            .bind(maintenance_run_id)
            .bind(cutoff_ts)
            .bind(sample_rate)
            .bind(archive_platforms)
            .bind(sample_rate)
            .bind(challenge_engagement)
            .bind(challenge_abs_signal)
            .bind(archive_platforms)
            .bind(challenge_engagement)
            .bind(challenge_abs_signal)
            .fetch_one(&self.pool)
            .await?;

        let rolled_up: i64 = row.try_get("rolled_up_buckets")?;
        let pruned: i64 = row.try_get("pruned_posts")?;

        Ok((rolled_up, pruned))
    }

    pub async fn rollup_and_prune_quotes(
        &self,
        cutoff_ts: DateTime<Utc>,
    ) -> Result<(i64, i64, i64)> {
        let row = sqlx::query(ROLLUP_AND_PRUNE_QUOTES_SQL)
            .bind(QUOTE_RETENTION_ADVISORY_LOCK_KEY)
            .bind(cutoff_ts)
            .fetch_one(&self.pool)
            .await?;

        let hourly_facts: i64 = row.try_get("hourly_facts")?;
        let daily_facts: i64 = row.try_get("daily_facts")?;
        let pruned_quotes: i64 = row.try_get("pruned_quotes")?;

        Ok((hourly_facts, daily_facts, pruned_quotes))
    }

    pub async fn prune_global_context(
        &self,
        hourly_cutoff: DateTime<Utc>,
        daily_cutoff: DateTime<Utc>,
        event_cutoff: DateTime<Utc>,
    ) -> Result<(u64, u64, u64)> {
        let mut tx = self.pool.begin().await?;

        let res_h =
            sqlx::query("DELETE FROM global_market_bars WHERE interval = '1h' AND ends_at < $1")
                .bind(hourly_cutoff)
                .execute(&mut *tx)
                .await?;

        let res_d =
            sqlx::query("DELETE FROM global_market_bars WHERE interval = '1d' AND ends_at < $1")
                .bind(daily_cutoff)
                .execute(&mut *tx)
                .await?;

        let res_e = sqlx::query("DELETE FROM global_event_signals WHERE occurred_at < $1")
            .bind(event_cutoff)
            .execute(&mut *tx)
            .await?;

        tx.commit().await?;

        Ok((
            res_h.rows_affected(),
            res_d.rows_affected(),
            res_e.rows_affected(),
        ))
    }
}
