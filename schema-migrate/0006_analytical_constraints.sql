-- Enforce the additive-statistics contract and make retained sampling policy
-- explicit enough for later bias-aware analysis.

ALTER TABLE hourly_sentiment_facts
    ADD CONSTRAINT hourly_sentiment_facts_bucket_aligned_ck CHECK (
        bucket_hour = date_trunc('hour', bucket_hour, 'UTC')
    ),
    ADD CONSTRAINT hourly_sentiment_facts_probability_ck CHECK (
        positive_probability_sum >= 0
        AND neutral_probability_sum >= 0
        AND negative_probability_sum >= 0
        AND positive_probability_sum <= post_count
        AND neutral_probability_sum <= post_count
        AND negative_probability_sum <= post_count
        AND ABS(
            positive_probability_sum + neutral_probability_sum
            + negative_probability_sum - post_count
        ) <= GREATEST(0.000001, post_count * 0.000001)
    ),
    ADD CONSTRAINT hourly_sentiment_facts_squares_ck CHECK (
        positive_probability_sq_sum >= 0
        AND neutral_probability_sq_sum >= 0
        AND negative_probability_sq_sum >= 0
        AND positive_probability_sq_sum <= positive_probability_sum + 0.000001
        AND neutral_probability_sq_sum <= neutral_probability_sum + 0.000001
        AND negative_probability_sq_sum <= negative_probability_sum + 0.000001
        AND signal_sq_sum >= 0
        AND signal_sq_sum <= post_count + 0.000001
        AND ABS(signal_sum) <= post_count + 0.000001
    ),
    ADD CONSTRAINT hourly_sentiment_facts_weights_ck CHECK (
        engagement_sum >= 0
        AND weight_sum >= 0
        AND positive_weight_sum >= 0
        AND neutral_weight_sum >= 0
        AND negative_weight_sum >= 0
        AND ABS(
            positive_weight_sum + neutral_weight_sum
            + negative_weight_sum - weight_sum
        ) <= GREATEST(0.000001, weight_sum * 0.000001)
        AND ABS(weighted_signal_sum) <= weight_sum + 0.000001
        AND weighted_signal_sq_sum >= 0
        AND weighted_signal_sq_sum <= weight_sum + 0.000001
    );

ALTER TABLE hourly_sentiment_fact_snapshots
    ADD CONSTRAINT hourly_sentiment_snapshots_bucket_aligned_ck CHECK (
        bucket_hour = date_trunc('hour', bucket_hour, 'UTC')
    ),
    ADD CONSTRAINT hourly_sentiment_snapshots_probability_ck CHECK (
        positive_probability_sum >= 0
        AND neutral_probability_sum >= 0
        AND negative_probability_sum >= 0
        AND positive_probability_sum <= post_count
        AND neutral_probability_sum <= post_count
        AND negative_probability_sum <= post_count
        AND ABS(
            positive_probability_sum + neutral_probability_sum
            + negative_probability_sum - post_count
        ) <= GREATEST(0.000001, post_count * 0.000001)
    ),
    ADD CONSTRAINT hourly_sentiment_snapshots_squares_ck CHECK (
        positive_probability_sq_sum >= 0
        AND neutral_probability_sq_sum >= 0
        AND negative_probability_sq_sum >= 0
        AND positive_probability_sq_sum <= positive_probability_sum + 0.000001
        AND neutral_probability_sq_sum <= neutral_probability_sum + 0.000001
        AND negative_probability_sq_sum <= negative_probability_sum + 0.000001
        AND signal_sq_sum >= 0
        AND signal_sq_sum <= post_count + 0.000001
        AND ABS(signal_sum) <= post_count + 0.000001
    ),
    ADD CONSTRAINT hourly_sentiment_snapshots_weights_ck CHECK (
        engagement_sum >= 0
        AND weight_sum >= 0
        AND positive_weight_sum >= 0
        AND neutral_weight_sum >= 0
        AND negative_weight_sum >= 0
        AND ABS(
            positive_weight_sum + neutral_weight_sum
            + negative_weight_sum - weight_sum
        ) <= GREATEST(0.000001, weight_sum * 0.000001)
        AND ABS(weighted_signal_sum) <= weight_sum + 0.000001
        AND weighted_signal_sq_sum >= 0
        AND weighted_signal_sq_sum <= weight_sum + 0.000001
    );

ALTER TABLE hourly_pipeline_quality_facts
    ADD CONSTRAINT hourly_pipeline_quality_bucket_aligned_ck CHECK (
        bucket_hour = date_trunc('hour', bucket_hour, 'UTC')
    );

ALTER TABLE raw_post_archive_staging
    ADD COLUMN sampling_policy_version TEXT NOT NULL DEFAULT 'sample-v1',
    ADD COLUMN stratum TEXT NOT NULL DEFAULT 'legacy';

COMMENT ON COLUMN raw_post_archive_staging.sampling_policy_version IS
    'Version of the deterministic probability/challenge selection rules.';
COMMENT ON COLUMN raw_post_archive_staging.stratum IS
    'Platform/model/topic/sentiment stratum known at selection time.';
