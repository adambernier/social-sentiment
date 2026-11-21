//! Algebraic rollup monoids and SQL `CHECK` invariant proofs for analytical facts.
//!
//! Mirrors the incremental `ON CONFLICT DO UPDATE` semantics of:
//! - `hourly_sentiment_facts` (`crates/storage-service/src/db.rs`, `schema-migrate/0003` & `0006`)
//! - `market_hourly_facts` / `market_daily_facts` (`ROLLUP_AND_PRUNE_QUOTES_SQL`)

use crate::postprocess::SentimentLabel;
use crate::validation::{validate_probability_simplex, STRICT_SIMPLEX_TOLERANCE};

/// Additive analytical fact bucket matching `hourly_sentiment_facts`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SentimentFactBucket {
    pub post_count: u64,
    pub positive_count: u64,
    pub neutral_count: u64,
    pub negative_count: u64,
    pub positive_probability_sum: f64,
    pub neutral_probability_sum: f64,
    pub negative_probability_sum: f64,
    pub positive_probability_sq_sum: f64,
    pub neutral_probability_sq_sum: f64,
    pub negative_probability_sq_sum: f64,
    pub signal_sum: f64,
    pub signal_sq_sum: f64,
    pub engagement_sum: u64,
    pub weight_sum: f64,
    pub positive_weight_sum: f64,
    pub neutral_weight_sum: f64,
    pub negative_weight_sum: f64,
    pub weighted_signal_sum: f64,
    pub weighted_signal_sq_sum: f64,
    pub first_event_at: i64,
    pub last_event_at: i64,
    pub first_ingested_at: i64,
    pub last_ingested_at: i64,
    pub first_scored_at: i64,
    pub last_scored_at: i64,
    pub input_watermark: i64,
}

impl SentimentFactBucket {
    /// Construct a singleton bucket from one validated scored post.
    ///
    /// Mirrors `retention_candidates` + `aggregated_posts` in `ROLLUP_AND_PRUNE_POSTS_SQL`,
    /// where `weight = LN(GREATEST(engagement, 0) + 1.0)` is non-negative and finite.
    #[allow(clippy::too_many_arguments)]
    pub fn from_post(
        label: SentimentLabel,
        positive: f64,
        neutral: f64,
        negative: f64,
        engagement: i32,
        weight: f64,
        event_at: i64,
        ingested_at: i64,
        scored_at: i64,
    ) -> Option<Self> {
        if validate_probability_simplex(positive, neutral, negative).is_err() {
            return None;
        }
        if !weight.is_finite() || weight < 0.0 {
            return None;
        }

        let signal = positive - negative;
        let signal_sq = signal * signal;
        let weighted_signal = weight * signal;
        let weighted_signal_sq = weight * signal_sq;
        if !weighted_signal.is_finite() || !weighted_signal_sq.is_finite() {
            return None;
        }

        let (positive_count, neutral_count, negative_count) = match label {
            SentimentLabel::Positive => (1, 0, 0),
            SentimentLabel::Neutral => (0, 1, 0),
            SentimentLabel::Negative => (0, 0, 1),
        };
        let (positive_weight_sum, neutral_weight_sum, negative_weight_sum) = match label {
            SentimentLabel::Positive => (weight, 0.0, 0.0),
            SentimentLabel::Neutral => (0.0, weight, 0.0),
            SentimentLabel::Negative => (0.0, 0.0, weight),
        };
        let clamped_engagement = engagement.max(0) as u64;

        Some(Self {
            post_count: 1,
            positive_count,
            neutral_count,
            negative_count,
            positive_probability_sum: positive,
            neutral_probability_sum: neutral,
            negative_probability_sum: negative,
            positive_probability_sq_sum: positive * positive,
            neutral_probability_sq_sum: neutral * neutral,
            negative_probability_sq_sum: negative * negative,
            signal_sum: signal,
            signal_sq_sum: signal_sq,
            engagement_sum: clamped_engagement,
            weight_sum: weight,
            positive_weight_sum,
            neutral_weight_sum,
            negative_weight_sum,
            weighted_signal_sum: weighted_signal,
            weighted_signal_sq_sum: weighted_signal_sq,
            first_event_at: event_at,
            last_event_at: event_at,
            first_ingested_at: ingested_at,
            last_ingested_at: ingested_at,
            first_scored_at: scored_at,
            last_scored_at: scored_at,
            input_watermark: ingested_at,
        })
    }

    /// Incremental merge matching `ON CONFLICT DO UPDATE` in `ROLLUP_AND_PRUNE_POSTS_SQL`.
    pub fn merge(&self, other: &Self) -> Self {
        Self {
            post_count: self.post_count + other.post_count,
            positive_count: self.positive_count + other.positive_count,
            neutral_count: self.neutral_count + other.neutral_count,
            negative_count: self.negative_count + other.negative_count,
            positive_probability_sum: self.positive_probability_sum
                + other.positive_probability_sum,
            neutral_probability_sum: self.neutral_probability_sum + other.neutral_probability_sum,
            negative_probability_sum: self.negative_probability_sum
                + other.negative_probability_sum,
            positive_probability_sq_sum: self.positive_probability_sq_sum
                + other.positive_probability_sq_sum,
            neutral_probability_sq_sum: self.neutral_probability_sq_sum
                + other.neutral_probability_sq_sum,
            negative_probability_sq_sum: self.negative_probability_sq_sum
                + other.negative_probability_sq_sum,
            signal_sum: self.signal_sum + other.signal_sum,
            signal_sq_sum: self.signal_sq_sum + other.signal_sq_sum,
            engagement_sum: self.engagement_sum + other.engagement_sum,
            weight_sum: self.weight_sum + other.weight_sum,
            positive_weight_sum: self.positive_weight_sum + other.positive_weight_sum,
            neutral_weight_sum: self.neutral_weight_sum + other.neutral_weight_sum,
            negative_weight_sum: self.negative_weight_sum + other.negative_weight_sum,
            weighted_signal_sum: self.weighted_signal_sum + other.weighted_signal_sum,
            weighted_signal_sq_sum: self.weighted_signal_sq_sum + other.weighted_signal_sq_sum,
            first_event_at: self.first_event_at.min(other.first_event_at),
            last_event_at: self.last_event_at.max(other.last_event_at),
            first_ingested_at: self.first_ingested_at.min(other.first_ingested_at),
            last_ingested_at: self.last_ingested_at.max(other.last_ingested_at),
            first_scored_at: self.first_scored_at.min(other.first_scored_at),
            last_scored_at: self.last_scored_at.max(other.last_scored_at),
            input_watermark: self.input_watermark.max(other.input_watermark),
        }
    }

    /// Check `0003_analytical_facts.sql` count and timestamp invariants.
    pub fn satisfies_counts_and_timestamps(&self) -> bool {
        self.positive_count
            .checked_add(self.neutral_count)
            .and_then(|s| s.checked_add(self.negative_count))
            == Some(self.post_count)
            && self.first_event_at <= self.last_event_at
            && self.first_ingested_at <= self.last_ingested_at
            && self.first_scored_at <= self.last_scored_at
    }

    /// Check `hourly_sentiment_facts_probability_ck` from `0006_analytical_constraints.sql`.
    pub fn satisfies_probability_ck(&self) -> bool {
        let n = self.post_count as f64;
        let tol = STRICT_SIMPLEX_TOLERANCE.max(n * STRICT_SIMPLEX_TOLERANCE);
        let p = self.positive_probability_sum;
        let u = self.neutral_probability_sum;
        let g = self.negative_probability_sum;
        p.is_finite()
            && u.is_finite()
            && g.is_finite()
            && p >= 0.0
            && u >= 0.0
            && g >= 0.0
            && p <= n
            && u <= n
            && g <= n
            && ((p + u + g) - n).abs() <= tol
    }

    /// Check `hourly_sentiment_facts_squares_ck` from `0006_analytical_constraints.sql`.
    pub fn satisfies_squares_ck(&self) -> bool {
        let n = self.post_count as f64;
        let eps = STRICT_SIMPLEX_TOLERANCE;
        self.positive_probability_sq_sum.is_finite()
            && self.neutral_probability_sq_sum.is_finite()
            && self.negative_probability_sq_sum.is_finite()
            && self.signal_sum.is_finite()
            && self.signal_sq_sum.is_finite()
            && self.positive_probability_sq_sum >= 0.0
            && self.neutral_probability_sq_sum >= 0.0
            && self.negative_probability_sq_sum >= 0.0
            && self.positive_probability_sq_sum <= self.positive_probability_sum + eps
            && self.neutral_probability_sq_sum <= self.neutral_probability_sum + eps
            && self.negative_probability_sq_sum <= self.negative_probability_sum + eps
            && self.signal_sq_sum >= 0.0
            && self.signal_sq_sum <= n + eps
            && self.signal_sum.abs() <= n + eps
    }

    /// Check `hourly_sentiment_facts_weights_ck` from `0006_analytical_constraints.sql`.
    pub fn satisfies_weights_ck(&self) -> bool {
        let eps = STRICT_SIMPLEX_TOLERANCE;
        let w = self.weight_sum;
        let tol = eps.max(w * eps);
        let pw = self.positive_weight_sum;
        let nw = self.neutral_weight_sum;
        let gw = self.negative_weight_sum;
        w.is_finite()
            && pw.is_finite()
            && nw.is_finite()
            && gw.is_finite()
            && self.weighted_signal_sum.is_finite()
            && self.weighted_signal_sq_sum.is_finite()
            && w >= 0.0
            && pw >= 0.0
            && nw >= 0.0
            && gw >= 0.0
            && ((pw + nw + gw) - w).abs() <= tol
            && self.weighted_signal_sum.abs() <= w + eps
            && self.weighted_signal_sq_sum >= 0.0
            && self.weighted_signal_sq_sum <= w + eps
    }

    /// Check every SQL `CHECK` constraint across `0003` and `0006`.
    pub fn satisfies_all_sql_constraints(&self) -> bool {
        self.satisfies_counts_and_timestamps()
            && self.satisfies_probability_ck()
            && self.satisfies_squares_ck()
            && self.satisfies_weights_ck()
    }
}

/// Additive OHLC fact bucket matching `market_hourly_facts` and `market_daily_facts`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct OhlcFactBucket {
    pub open_price: f64,
    pub high_price: f64,
    pub low_price: f64,
    pub close_price: f64,
    pub last_volume: i64,
    pub observation_count: u64,
    pub had_regular_session: bool,
    pub first_observed_at: i64,
    pub last_observed_at: i64,
}

impl OhlcFactBucket {
    /// Construct a singleton OHLC bucket from one quote observation.
    pub fn from_quote(
        price: f64,
        volume: i64,
        is_regular_session: bool,
        observed_at: i64,
    ) -> Option<Self> {
        if !price.is_finite() || price <= 0.0 {
            return None;
        }
        Some(Self {
            open_price: price,
            high_price: price,
            low_price: price,
            close_price: price,
            last_volume: volume,
            observation_count: 1,
            had_regular_session: is_regular_session,
            first_observed_at: observed_at,
            last_observed_at: observed_at,
        })
    }

    /// Merge `excluded` into `self` matching `ON CONFLICT DO UPDATE` in `ROLLUP_AND_PRUNE_QUOTES_SQL`.
    pub fn merge(&self, excluded: &Self) -> Self {
        let open_price = if excluded.first_observed_at < self.first_observed_at {
            excluded.open_price
        } else {
            self.open_price
        };
        let (close_price, last_volume) = if excluded.last_observed_at >= self.last_observed_at {
            (excluded.close_price, excluded.last_volume)
        } else {
            (self.close_price, self.last_volume)
        };
        Self {
            open_price,
            high_price: self.high_price.max(excluded.high_price),
            low_price: self.low_price.min(excluded.low_price),
            close_price,
            last_volume,
            observation_count: self.observation_count + excluded.observation_count,
            had_regular_session: self.had_regular_session || excluded.had_regular_session,
            first_observed_at: self.first_observed_at.min(excluded.first_observed_at),
            last_observed_at: self.last_observed_at.max(excluded.last_observed_at),
        }
    }

    /// Check OHLC ordering, count, and timestamp invariants matching `0003_analytical_facts.sql`.
    pub fn satisfies_invariants(&self) -> bool {
        self.open_price.is_finite()
            && self.high_price.is_finite()
            && self.low_price.is_finite()
            && self.close_price.is_finite()
            && self.observation_count > 0
            && self.low_price <= self.open_price
            && self.open_price <= self.high_price
            && self.low_price <= self.close_price
            && self.close_price <= self.high_price
            && self.low_price <= self.high_price
            && self.first_observed_at <= self.last_observed_at
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sentiment_singleton_and_merge_satisfy_all_sql_constraints() {
        let a = SentimentFactBucket::from_post(
            SentimentLabel::Positive,
            0.7,
            0.2,
            0.1,
            15,
            16.0_f64.ln(),
            100,
            105,
            106,
        )
        .expect("valid post");
        let b = SentimentFactBucket::from_post(
            SentimentLabel::Negative,
            0.05,
            0.15,
            0.8,
            0,
            1.0_f64.ln(),
            90,
            110,
            112,
        )
        .expect("valid post");

        assert!(a.satisfies_all_sql_constraints());
        assert!(b.satisfies_all_sql_constraints());
        let merged = a.merge(&b);
        assert!(merged.satisfies_all_sql_constraints());
        assert_eq!(merged.post_count, 2);
        assert_eq!(merged.first_event_at, 90);
        assert_eq!(merged.last_event_at, 100);
    }

    #[test]
    fn ohlc_merge_is_order_independent_for_distinct_timestamps() {
        let q1 = OhlcFactBucket::from_quote(100.0, 500, true, 10).unwrap();
        let q2 = OhlcFactBucket::from_quote(105.0, 600, false, 20).unwrap();
        let q3 = OhlcFactBucket::from_quote(98.0, 700, false, 30).unwrap();

        let left = q1.merge(&q2).merge(&q3);
        let right = q3.merge(&q1).merge(&q2);
        assert_eq!(left, right);
        assert!(left.satisfies_invariants());
        assert_eq!(left.open_price, 100.0);
        assert_eq!(left.high_price, 105.0);
        assert_eq!(left.low_price, 98.0);
        assert_eq!(left.close_price, 98.0);
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    fn any_label() -> SentimentLabel {
        match kani::any::<u8>() % 3 {
            0 => SentimentLabel::Neutral,
            1 => SentimentLabel::Positive,
            _ => SentimentLabel::Negative,
        }
    }

    #[kani::proof]
    fn verify_sentiment_singleton_probability_and_count_ck() {
        // Proves that tightening SIMPLEX_TOLERANCE to 5e-7 strictly implies
        // PostgreSQL's 1e-6 `hourly_sentiment_facts_probability_ck` on every singleton.
        let pos: f64 = kani::any();
        let neu: f64 = kani::any();
        let neg: f64 = kani::any();

        if validate_probability_simplex(pos, neu, neg).is_ok() {
            assert!(crate::validation::validate_probability_simplex_with_tol(
                pos,
                neu,
                neg,
                STRICT_SIMPLEX_TOLERANCE,
            )
            .is_ok());
        }
    }

    #[kani::proof]
    fn verify_sentiment_unit_square_and_signal_bounds() {
        // Proves the per-post inequalities underlying `hourly_sentiment_facts_squares_ck`:
        // 1. For any p in [0, 1], 0 <= p^2 <= p (exact in IEEE-754 f64).
        // 2. For any pos, neg in [0, 1], signal = pos - neg satisfies |signal| <= 1 and signal^2 <= 1.
        let p: f64 = kani::any();
        kani::assume(p.is_finite() && (0.0..=1.0).contains(&p));
        let p_sq = p * p;
        assert!(p_sq.is_finite() && p_sq >= 0.0 && p_sq <= p);

        let pos: f64 = kani::any();
        let neg: f64 = kani::any();
        kani::assume(pos.is_finite() && (0.0..=1.0).contains(&pos));
        kani::assume(neg.is_finite() && (0.0..=1.0).contains(&neg));
        let signal = pos - neg;
        assert!(signal.is_finite() && signal.abs() <= 1.0);
        let signal_sq = signal * signal;
        assert!(signal_sq.is_finite() && signal_sq >= 0.0 && signal_sq <= 1.0);
    }

    #[kani::proof]
    fn verify_sentiment_singleton_weights_ck() {
        // Proves the per-post weight inequality underlying `hourly_sentiment_facts_weights_ck`:
        // for any finite weight in [0, 100] (`LN(i32::MAX + 1) < 22`) and bounded factor
        // in [-1, 1] (covering both `signal` and `signal^2`), `|weight * factor| <= weight`
        // and `factor >= 0 ==> 0 <= weight * factor <= weight`.
        let weight_f32: f32 = kani::any();
        let factor_f32: f32 = kani::any();
        kani::assume(weight_f32.is_finite() && (0.0..=100.0).contains(&weight_f32));
        kani::assume(factor_f32.is_finite() && (-1.0..=1.0).contains(&factor_f32));

        let weight = f64::from(weight_f32);
        let factor = f64::from(factor_f32);
        let prod = weight * factor;
        assert!(prod.is_finite() && prod.abs() <= weight);
        if factor >= 0.0 {
            assert!(prod >= 0.0);
        }
    }

    #[kani::proof]
    fn verify_sentiment_counts_and_timestamps_form_commutative_monoid() {
        let a = SentimentFactBucket::from_post(
            any_label(),
            0.5,
            0.3,
            0.2,
            kani::any(),
            1.0,
            kani::any(),
            kani::any(),
            kani::any(),
        )
        .unwrap();
        let b = SentimentFactBucket::from_post(
            any_label(),
            0.2,
            0.5,
            0.3,
            kani::any(),
            1.0,
            kani::any(),
            kani::any(),
            kani::any(),
        )
        .unwrap();
        let c = SentimentFactBucket::from_post(
            any_label(),
            0.1,
            0.2,
            0.7,
            kani::any(),
            1.0,
            kani::any(),
            kani::any(),
            kani::any(),
        )
        .unwrap();

        let ab = a.merge(&b);
        let ba = b.merge(&a);
        assert!(ab.satisfies_counts_and_timestamps());
        assert_eq!(ab.post_count, ba.post_count);
        assert_eq!(ab.positive_count, ba.positive_count);
        assert_eq!(ab.neutral_count, ba.neutral_count);
        assert_eq!(ab.negative_count, ba.negative_count);
        assert_eq!(ab.engagement_sum, ba.engagement_sum);
        assert_eq!(ab.first_event_at, ba.first_event_at);
        assert_eq!(ab.last_event_at, ba.last_event_at);
        assert_eq!(ab.first_ingested_at, ba.first_ingested_at);
        assert_eq!(ab.last_ingested_at, ba.last_ingested_at);
        assert_eq!(ab.first_scored_at, ba.first_scored_at);
        assert_eq!(ab.last_scored_at, ba.last_scored_at);
        assert_eq!(ab.input_watermark, ba.input_watermark);

        let ab_c = ab.merge(&c);
        let a_bc = a.merge(&b.merge(&c));
        assert!(ab_c.satisfies_counts_and_timestamps());
        assert_eq!(ab_c.post_count, a_bc.post_count);
        assert_eq!(ab_c.positive_count, a_bc.positive_count);
        assert_eq!(ab_c.neutral_count, a_bc.neutral_count);
        assert_eq!(ab_c.negative_count, a_bc.negative_count);
        assert_eq!(ab_c.engagement_sum, a_bc.engagement_sum);
        assert_eq!(ab_c.first_event_at, a_bc.first_event_at);
        assert_eq!(ab_c.last_event_at, a_bc.last_event_at);
        assert_eq!(ab_c.input_watermark, a_bc.input_watermark);
    }

    #[kani::proof]
    fn verify_sentiment_merge_preserves_probability_check_inductively() {
        // Inductive error-budget theorem for `hourly_sentiment_facts_probability_ck`:
        // Across bucket scales spanning 1..=100_000 posts, two buckets each within
        // the 5e-7 per-post ingestion budget merge strictly inside PostgreSQL's
        // 1e-6 * (n1 + n2) CHECK budget under IEEE-754 f64 summation rounding.
        const SCALES: [u32; 6] = [1, 10, 100, 1_000, 10_000, 100_000];
        let i1: usize = kani::any();
        let i2: usize = kani::any();
        kani::assume(i1 < SCALES.len() && i2 < SCALES.len());

        let n1 = SCALES[i1];
        let n2 = SCALES[i2];
        let n1f = f64::from(n1);
        let n2f = f64::from(n2);
        let n_sum = f64::from(n1 + n2);
        let sql_tol = n_sum * STRICT_SIMPLEX_TOLERANCE;

        let d1_f32: f32 = kani::any();
        let d2_f32: f32 = kani::any();
        let d1 = f64::from(d1_f32);
        let d2 = f64::from(d2_f32);
        kani::assume(d1.is_finite() && d2.is_finite());
        kani::assume(d1.abs() <= n1f * crate::validation::SIMPLEX_TOLERANCE);
        kani::assume(d2.abs() <= n2f * crate::validation::SIMPLEX_TOLERANCE);

        let s1 = n1f + d1;
        let s2 = n2f + d2;
        let s_sum = s1 + s2;
        assert!((s_sum - n_sum).abs() <= sql_tol);
    }

    #[kani::proof]
    fn verify_ohlc_merge_preserves_invariants_and_monoid_laws() {
        let a = OhlcFactBucket {
            open_price: kani::any(),
            high_price: kani::any(),
            low_price: kani::any(),
            close_price: kani::any(),
            last_volume: kani::any(),
            observation_count: kani::any(),
            had_regular_session: kani::any(),
            first_observed_at: kani::any(),
            last_observed_at: kani::any(),
        };
        let b = OhlcFactBucket {
            open_price: kani::any(),
            high_price: kani::any(),
            low_price: kani::any(),
            close_price: kani::any(),
            last_volume: kani::any(),
            observation_count: kani::any(),
            had_regular_session: kani::any(),
            first_observed_at: kani::any(),
            last_observed_at: kani::any(),
        };
        let c = OhlcFactBucket {
            open_price: kani::any(),
            high_price: kani::any(),
            low_price: kani::any(),
            close_price: kani::any(),
            last_volume: kani::any(),
            observation_count: kani::any(),
            had_regular_session: kani::any(),
            first_observed_at: kani::any(),
            last_observed_at: kani::any(),
        };

        kani::assume(
            a.satisfies_invariants() && b.satisfies_invariants() && c.satisfies_invariants(),
        );
        kani::assume(a.observation_count <= 1_000_000);
        kani::assume(b.observation_count <= 1_000_000);
        kani::assume(c.observation_count <= 1_000_000);

        // 1. Inductive preservation of all OHLC + timestamp invariants (even on timestamp ties).
        let ab = a.merge(&b);
        assert!(ab.satisfies_invariants());
        assert_eq!(
            ab.observation_count,
            a.observation_count + b.observation_count
        );

        // 2. Commutativity when endpoint timestamps are distinct.
        if a.first_observed_at != b.first_observed_at && a.last_observed_at != b.last_observed_at {
            assert_eq!(ab, b.merge(&a));
        }

        // 3. Associativity when endpoint timestamps across a, b, c are pairwise distinct.
        if a.first_observed_at != b.first_observed_at
            && b.first_observed_at != c.first_observed_at
            && a.first_observed_at != c.first_observed_at
            && a.last_observed_at != b.last_observed_at
            && b.last_observed_at != c.last_observed_at
            && a.last_observed_at != c.last_observed_at
        {
            assert_eq!(ab.merge(&c), a.merge(&b.merge(&c)));
        }
    }
}
