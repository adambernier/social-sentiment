import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    DATABASE_DSN,
    RAW_ARCHIVE_CHALLENGE_ABS_SIGNAL,
    RAW_ARCHIVE_CHALLENGE_ENGAGEMENT,
    RAW_ARCHIVE_PLATFORMS,
    RAW_ARCHIVE_SAMPLE_RATE,
)
from shared.global_context import NormalizedMarketBar
from shared.global_instrument_catalog import (
    CatalogInstrument,
    CatalogSymbolExposures,
)
from shared.schemas import ScoredPost, StockMetrics, StockQuote

# Serializes post-retention maintenance across scheduler/manual invocations. The
# transaction-scoped lock is acquired inside the atomic move statement and is
# released as soon as that statement commits or rolls back.
POST_RETENTION_ADVISORY_LOCK_KEY = 4815162343
QUOTE_RETENTION_ADVISORY_LOCK_KEY = 4815162344

INSERT_POST_SQL = """
    INSERT INTO posts (
        id, symbol, platform, text, timestamp, sentiment, scores,
        topic_id, topic_label, engagement, ingested_at, cleaned_at,
        topic_scored_at, sentiment_scored_at, engagement_observed_at,
        source_schema_version, pipeline_git_commit, topic_model_version,
        topic_model_hash, sentiment_model_version, sentiment_model_hash
    )
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (platform, id, symbol) DO NOTHING
    RETURNING id
"""

INSERT_QUOTE_SQL = """
    INSERT INTO stock_quotes (
        symbol, timestamp, price, volume, market_session, provider
    )
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (symbol, timestamp) DO NOTHING
    RETURNING id
"""

UPSERT_PIPELINE_QUALITY_SQL = """
    INSERT INTO hourly_pipeline_quality_facts (
        bucket_hour, pipeline_stage, platform, reason, pipeline_version,
        count, first_observed_at, last_observed_at
    )
    VALUES (
        date_trunc('hour', NOW()), 'storage', %s,
        'duplicate_or_conflict', 'storage-insert-v1', %s, NOW(), NOW()
    )
    ON CONFLICT (
        bucket_hour, pipeline_stage, platform, reason, pipeline_version
    ) DO UPDATE SET
        count = hourly_pipeline_quality_facts.count + EXCLUDED.count,
        last_observed_at = EXCLUDED.last_observed_at,
        computed_at = NOW()
"""

UPSERT_METRICS_SQL = """
    INSERT INTO stock_metrics (
        symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y,
        pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (symbol) DO UPDATE SET
        pe_ratio = EXCLUDED.pe_ratio,
        beta = EXCLUDED.beta,
        avg_return_1y = EXCLUDED.avg_return_1y,
        inflation_adj_return_1y = EXCLUDED.inflation_adj_return_1y,
        pe_relative_sector = EXCLUDED.pe_relative_sector,
        beta_relative_sector = EXCLUDED.beta_relative_sector,
        return_relative_sector = EXCLUDED.return_relative_sector,
        updated_at = NOW()
    RETURNING symbol
"""

UPSERT_GLOBAL_BAR_SQL = """
    INSERT INTO global_market_bars (
        instrument_key, interval, starts_at, ends_at, session_date,
        open_price, high_price, low_price, close_price, volume, provider,
        fetched_at
    )
    VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s,
        NOW()
    )
    ON CONFLICT (instrument_key, interval, starts_at) DO UPDATE SET
        ends_at = EXCLUDED.ends_at,
        session_date = EXCLUDED.session_date,
        open_price = EXCLUDED.open_price,
        high_price = EXCLUDED.high_price,
        low_price = EXCLUDED.low_price,
        close_price = EXCLUDED.close_price,
        volume = EXCLUDED.volume,
        provider = EXCLUDED.provider,
        fetched_at = NOW()
"""

UPSERT_GLOBAL_INSTRUMENT_SQL = """
    INSERT INTO global_instruments (
        instrument_key, display_name, asset_class, currency, exchange,
        timezone, provider_aliases, session_metadata, quote_convention,
        is_active
    )
    VALUES (
        %s, %s, %s, %s, %s,
        %s, %s::jsonb, %s::jsonb, %s,
        %s
    )
    ON CONFLICT (instrument_key) DO UPDATE SET
        display_name = EXCLUDED.display_name,
        asset_class = EXCLUDED.asset_class,
        currency = EXCLUDED.currency,
        exchange = EXCLUDED.exchange,
        timezone = EXCLUDED.timezone,
        provider_aliases = EXCLUDED.provider_aliases,
        session_metadata = EXCLUDED.session_metadata,
        quote_convention = EXCLUDED.quote_convention,
        is_active = EXCLUDED.is_active,
        updated_at = NOW()
"""

UPSERT_STOCK_FACTOR_EXPOSURE_SQL = """
    INSERT INTO stock_factor_exposures (
        symbol, instrument_key, reason, display_order
    )
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (symbol, instrument_key) DO UPDATE SET
        reason = EXCLUDED.reason,
        display_order = EXCLUDED.display_order,
        updated_at = NOW()
"""

ROLLUP_AND_PRUNE_POSTS_SQL = """
    WITH maintenance_run AS MATERIALIZED (
        SELECT
            pg_advisory_xact_lock(%s),
            %s::uuid AS run_id,
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
        WHERE p.timestamp < %s
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
            %s::DOUBLE PRECISION,
            ARRAY['deterministic_uniform_sample']::TEXT[],
            'sample-v1',
            CONCAT_WS(
                ':', c.platform, c.topic_model_hash, c.topic_id,
                c.sentiment_model_hash, c.sentiment
            )
        FROM retention_candidates AS c
        WHERE c.platform = ANY(%s::TEXT[])
          AND (
              ('x' || SUBSTR(
                  MD5(CONCAT_WS(
                      CHR(31), 'sample-v1', c.platform, c.id, c.symbol
                  )),
                  1,
                  8
              ))::BIT(32)::BIGINT / 4294967296.0
          ) < %s::DOUBLE PRECISION

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
                    CASE WHEN c.engagement >= %s
                        THEN 'high_engagement' END,
                    CASE WHEN ABS(c.signal) >= %s
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
        WHERE c.platform = ANY(%s::TEXT[])
          AND (c.engagement >= %s OR ABS(c.signal) >= %s)
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
"""

ROLLUP_AND_PRUNE_QUOTES_SQL = """
    WITH maintenance_lock AS MATERIALIZED (
        SELECT pg_advisory_xact_lock(%s)
    ),
    deleted_quotes AS (
        DELETE FROM stock_quotes
        WHERE timestamp < %s
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
"""


def _post_values(post: ScoredPost) -> tuple:
    return (
        post.id,
        post.symbol,
        post.platform,
        post.text.replace("\x00", "") if post.text else "",
        post.timestamp,
        post.sentiment,
        json.dumps(post.scores),
        post.topic_id if post.topic_id is not None else -2,
        post.topic_label.replace("\x00", "") if post.topic_label else None,
        post.engagement,
        post.ingested_at,
        post.cleaned_at,
        post.topic_scored_at,
        post.sentiment_scored_at,
        post.engagement_observed_at,
        post.source_schema_version,
        post.pipeline_git_commit,
        post.topic_model_version,
        post.topic_model_hash,
        post.sentiment_model_version,
        post.sentiment_model_hash,
    )


def _batch_platform(posts: list[ScoredPost]) -> str:
    platforms = {post.platform for post in posts}
    return next(iter(platforms)) if len(platforms) == 1 else "mixed"


class DB:
    def __init__(self, dsn: str = DATABASE_DSN):
        self.dsn = dsn
        self.conn: psycopg.Connection | None = None
        self.async_pool: AsyncConnectionPool | None = None
        self._connect()

    def _connect(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except psycopg.Error as exc:
                print(f"Error closing stale database connection: {exc}")
        self.conn = psycopg.connect(self.dsn, autocommit=True)

    async def get_async_pool(self) -> AsyncConnectionPool:
        if self.async_pool is None:
            self.async_pool = AsyncConnectionPool(
                self.dsn, min_size=2, max_size=10, open=False
            )
            await self.async_pool.open()
        return self.async_pool

    async def insert_scored_batch_async(self, posts: list[ScoredPost]) -> int:
        data = [_post_values(post) for post in posts]
        pool = await self.get_async_pool()
        async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(INSERT_POST_SQL, data)
            inserted_count = max(cur.rowcount, 0)
            duplicate_count = max(len(posts) - inserted_count, 0)
            if duplicate_count:
                await cur.execute(
                    UPSERT_PIPELINE_QUALITY_SQL,
                    (_batch_platform(posts), duplicate_count),
                )
            return inserted_count

    def insert_scored_batch(self, posts: list[ScoredPost]) -> int:
        try:
            return self._do_insert_posts_batch(posts)
        except psycopg.OperationalError:
            print("DB connection lost, reconnecting...")
            self._connect()
            return self._do_insert_posts_batch(posts)

    def _do_insert_posts_batch(self, posts: list[ScoredPost]) -> int:
        data = [_post_values(post) for post in posts]
        assert self.conn is not None
        with self.conn.transaction(), self.conn.cursor() as cur:
            # executemany is efficient for small-to-medium batches
            cur.executemany(INSERT_POST_SQL, data)
            inserted_count = max(cur.rowcount, 0)
            duplicate_count = max(len(posts) - inserted_count, 0)
            if duplicate_count:
                cur.execute(
                    UPSERT_PIPELINE_QUALITY_SQL,
                    (_batch_platform(posts), duplicate_count),
                )
            return inserted_count

    def insert_quote(self, quote: StockQuote) -> bool:
        try:
            return self._do_insert_quote(quote)
        except psycopg.OperationalError:
            print("DB connection lost, reconnecting...")
            self._connect()
            return self._do_insert_quote(quote)

    def _do_insert_quote(self, quote: StockQuote) -> bool:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                INSERT_QUOTE_SQL,
                (
                    quote.symbol,
                    quote.timestamp,
                    quote.price,
                    quote.volume,
                    quote.market_session,
                    quote.provider,
                ),
            )
            return cur.fetchone() is not None

    def upsert_metrics(self, metrics: StockMetrics) -> bool:
        try:
            return self._do_upsert_metrics(metrics)
        except psycopg.OperationalError:
            print("DB connection lost, reconnecting...")
            self._connect()
            return self._do_upsert_metrics(metrics)

    def _do_upsert_metrics(self, metrics: StockMetrics) -> bool:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                UPSERT_METRICS_SQL,
                (
                    metrics.symbol,
                    metrics.pe_ratio,
                    metrics.beta,
                    metrics.avg_return_1y,
                    metrics.inflation_adj_return_1y,
                    metrics.pe_relative_sector,
                    metrics.beta_relative_sector,
                    metrics.return_relative_sector,
                ),
            )
            return cur.fetchone() is not None

    def ensure_us_equity_instruments(self, symbols: list[str]) -> None:
        """Create provider-neutral reference instruments for tracked U.S. stocks."""
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol})
        if not normalized:
            return
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO global_instruments (
                    instrument_key, display_name, asset_class, currency,
                    exchange, timezone, provider_aliases, session_metadata
                )
                VALUES (
                    %s, %s, 'us_equity', 'USD', 'NYSE/Nasdaq',
                    'America/New_York', %s::jsonb,
                    '{"open":"09:30","close":"16:00",'
                    '"weekdays":[1,2,3,4,5]}'::jsonb
                )
                ON CONFLICT (instrument_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    provider_aliases = EXCLUDED.provider_aliases,
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                [
                    (
                        f"us-stock:{symbol}",
                        symbol,
                        json.dumps({"yahoo": symbol}),
                    )
                    for symbol in normalized
                ],
            )

    def sync_global_instrument_catalog(
        self,
        instruments: list[CatalogInstrument],
    ) -> int:
        """Atomically upsert catalog entries without deleting unlisted rows."""
        if not instruments:
            return 0
        assert self.conn is not None
        values = [
            (
                instrument.instrument_key,
                instrument.display_name,
                instrument.asset_class,
                instrument.currency,
                instrument.exchange,
                instrument.timezone,
                json.dumps(instrument.provider_aliases),
                json.dumps(instrument.session_metadata),
                instrument.quote_convention,
                instrument.is_active,
            )
            for instrument in instruments
        ]
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_INSTRUMENT_SQL, values)
        except psycopg.OperationalError:
            # The upsert is idempotent, so retrying after an unknown commit
            # outcome is safe.
            self._connect()
            assert self.conn is not None
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_INSTRUMENT_SQL, values)
        return len(values)

    def sync_stock_factor_exposures(
        self,
        symbol_exposures: list[CatalogSymbolExposures],
    ) -> int:
        if not symbol_exposures:
            return 0
        assert self.conn is not None
        values = [
            (
                entry.symbol,
                exp.instrument_key,
                exp.reason,
                exp.display_order,
            )
            for entry in symbol_exposures
            for exp in entry.exposures
        ]
        if not values:
            return 0
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(UPSERT_STOCK_FACTOR_EXPOSURE_SQL, values)
        except psycopg.OperationalError:
            self._connect()
            assert self.conn is not None
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(UPSERT_STOCK_FACTOR_EXPOSURE_SQL, values)
        return len(values)

    def list_active_symbols(self) -> list[str]:
        """Return active tracked symbols directly from the source-of-truth table."""
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol
                FROM tracked_symbols
                WHERE is_active
                ORDER BY symbol
                """
            )
            return [row[0] for row in cur.fetchall()]

    def list_global_instruments(self) -> list[dict]:
        assert self.conn is not None
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT instrument_key, display_name, asset_class, currency,
                       exchange, timezone, provider_aliases, session_metadata,
                       quote_convention
                FROM global_instruments
                WHERE is_active
                ORDER BY asset_class, instrument_key
                """
            )
            return cur.fetchall()

    def upsert_global_bars(
        self,
        bars: list[NormalizedMarketBar],
    ) -> int:
        if not bars:
            return 0
        assert self.conn is not None
        values = [
            (
                bar.instrument_key,
                bar.interval,
                bar.starts_at,
                bar.ends_at,
                bar.session_date,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
                bar.volume,
                bar.provider,
            )
            for bar in bars
        ]
        try:
            with self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_BAR_SQL, values)
        except psycopg.OperationalError:
            print("DB connection lost during global bar upsert, reconnecting...")
            self._connect()
            assert self.conn is not None
            with self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_BAR_SQL, values)
        return len(values)

    def rollup_and_prune_posts(
        self,
        cutoff_ts,
        *,
        archive_platforms: tuple[str, ...] = RAW_ARCHIVE_PLATFORMS,
        sample_rate: float = RAW_ARCHIVE_SAMPLE_RATE,
        challenge_engagement: int = RAW_ARCHIVE_CHALLENGE_ENGAGEMENT,
        challenge_abs_signal: float = RAW_ARCHIVE_CHALLENGE_ABS_SIGNAL,
    ) -> tuple[int, int]:
        """Atomically aggregate and delete posts older than ``cutoff_ts``.

        The aggregate is derived from the exact rows returned by DELETE. Existing
        buckets are incremented so late-arriving posts are retained. Returns
        ``(rolled_up_buckets, pruned_posts)``.
        """
        try:
            return self._do_rollup_and_prune_posts(
                cutoff_ts,
                archive_platforms=archive_platforms,
                sample_rate=sample_rate,
                challenge_engagement=challenge_engagement,
                challenge_abs_signal=challenge_abs_signal,
            )
        except psycopg.OperationalError:
            # Retrying is safe even when commit status is unknown: if the first
            # statement committed, its source posts no longer exist; otherwise
            # PostgreSQL rolled back both the delete and aggregate upsert.
            print("DB connection lost during post retention, reconnecting...")
            self._connect()
            return self._do_rollup_and_prune_posts(
                cutoff_ts,
                archive_platforms=archive_platforms,
                sample_rate=sample_rate,
                challenge_engagement=challenge_engagement,
                challenge_abs_signal=challenge_abs_signal,
            )

    def _do_rollup_and_prune_posts(
        self,
        cutoff_ts,
        *,
        archive_platforms: tuple[str, ...],
        sample_rate: float,
        challenge_engagement: int,
        challenge_abs_signal: float,
    ) -> tuple[int, int]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            maintenance_run_id = uuid.uuid4()
            platforms = list(archive_platforms)
            # PostgreSQL cannot attach a partition while the same statement is
            # already planning a write to the parent. This idempotent preflight
            # does no row mutation; a failure leaves every source post intact.
            cur.execute(
                """
                SELECT
                    pg_advisory_xact_lock(%s),
                    ensure_hourly_analytical_partitions(
                        COALESCE(
                            (SELECT MIN(timestamp) FROM posts
                             WHERE timestamp < %s),
                            %s
                        ),
                        NOW() + INTERVAL '3 months'
                    )
                """,
                [POST_RETENTION_ADVISORY_LOCK_KEY, cutoff_ts, cutoff_ts],
            )
            cur.execute(
                ROLLUP_AND_PRUNE_POSTS_SQL,
                [
                    POST_RETENTION_ADVISORY_LOCK_KEY,
                    maintenance_run_id,
                    cutoff_ts,
                    sample_rate,
                    platforms,
                    sample_rate,
                    challenge_engagement,
                    challenge_abs_signal,
                    platforms,
                    challenge_engagement,
                    challenge_abs_signal,
                ],
            )
            result = cur.fetchone()
            assert result is not None
            return result[0], result[1]

    def rollup_and_prune_quotes(self, cutoff_ts) -> tuple[int, int, int]:
        """Atomically preserve hourly/daily OHLC facts and delete old quotes."""
        assert self.conn is not None
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    ROLLUP_AND_PRUNE_QUOTES_SQL,
                    [QUOTE_RETENTION_ADVISORY_LOCK_KEY, cutoff_ts],
                )
                result = cur.fetchone()
                assert result is not None
                return result
        except psycopg.OperationalError:
            print("DB connection lost during quote retention, reconnecting...")
            self._connect()
            return self.rollup_and_prune_quotes(cutoff_ts)

    def prune_old_quotes(self, cutoff_ts) -> int:
        """Compatibility wrapper returning only the number of deleted quotes."""
        return self.rollup_and_prune_quotes(cutoff_ts)[2]

    def prune_global_context(
        self,
        *,
        hourly_cutoff: datetime,
        daily_cutoff: datetime,
        event_cutoff: datetime,
    ) -> tuple[int, int, int]:
        """Apply the 180-day/5-year/1-year context retention policy."""
        assert self.conn is not None
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM global_market_bars
                WHERE interval = '1h' AND ends_at < %s
                """,
                [hourly_cutoff],
            )
            hourly = cur.rowcount
            cur.execute(
                """
                DELETE FROM global_market_bars
                WHERE interval = '1d' AND ends_at < %s
                """,
                [daily_cutoff],
            )
            daily = cur.rowcount
            cur.execute(
                """
                DELETE FROM global_event_signals
                WHERE occurred_at < %s
                """,
                [event_cutoff],
            )
            events = cur.rowcount
        return hourly, daily, events
