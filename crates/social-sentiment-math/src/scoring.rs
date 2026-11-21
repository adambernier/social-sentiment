//! Opportunity scoring, options strategy classification, and forward signal evaluation.

use crate::market::SectorRelativeScore;

/// Support proximity band around the support price (`±2.5%`).
pub const SUPPORT_NEAR_BAND: f64 = 0.025;

/// Upper approach band above support (`(2.5%, 5.0%]`).
pub const SUPPORT_APPROACH_MAX: f64 = 0.05;

/// Forward return threshold (`+0.2%`) required for a buy signal to be marked `Good`.
pub const SIGNAL_GOOD_RETURN_MULTIPLIER: f64 = 1.002;

/// Discrete opportunity classification tier.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpportunityClass {
    StrongBuy,
    Accumulate,
    HoldNeutral,
    CautionOverbought,
}

impl OpportunityClass {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::StrongBuy => "STRONG BUY",
            Self::Accumulate => "ACCUMULATE",
            Self::HoldNeutral => "HOLD / NEUTRAL",
            Self::CautionOverbought => "CAUTION / OVERBOUGHT",
        }
    }

    pub const fn color(self) -> &'static str {
        match self {
            Self::StrongBuy => "emerald",
            Self::Accumulate => "teal",
            Self::HoldNeutral => "slate",
            Self::CautionOverbought => "rose",
        }
    }

    /// Single source of truth for whether an opportunity class triggers a buy signal.
    pub const fn is_buy_signal(self) -> bool {
        matches!(self, Self::StrongBuy | Self::Accumulate)
    }
}

/// Discrete options/equity strategy recommendation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpportunityStrategy {
    LongCallOption,
    LongSharesOrBullCallSpread,
    SellPutCreditSpreads,
    HoldOrSellCoveredCalls,
    ProtectLongsOrBuyPuts,
    SellCallCreditSpreads,
}

impl OpportunityStrategy {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::LongCallOption => "Long Call Option (Buy Call)",
            Self::LongSharesOrBullCallSpread => "Long Shares / Bull Call Spread",
            Self::SellPutCreditSpreads => "Sell Put Credit Spreads",
            Self::HoldOrSellCoveredCalls => "Hold / Sell Covered Calls",
            Self::ProtectLongsOrBuyPuts => "Protect Longs / Buy Puts",
            Self::SellCallCreditSpreads => "Sell Call Credit Spreads",
        }
    }

    pub const fn description(self) -> &'static str {
        match self {
            Self::LongCallOption => {
                "Low volatility regime. Buy outright call options to leverage cheap premium."
            }
            Self::LongSharesOrBullCallSpread => {
                "Moderate volatility. Buy shares directly or use bull call debit spreads."
            }
            Self::SellPutCreditSpreads => {
                "High volatility regime. Sell put credit spreads to harvest rich premium."
            }
            Self::HoldOrSellCoveredCalls => {
                "Neutral market stance. Capitalize on range-bound behavior using covered call writing."
            }
            Self::ProtectLongsOrBuyPuts => {
                "Overbought stance. Trim shares or buy puts to hedge downside risk."
            }
            Self::SellCallCreditSpreads => {
                "High volatility overbought. Sell call credit spreads for fast premium decay."
            }
        }
    }

    pub const fn is_bullish(self) -> bool {
        matches!(
            self,
            Self::LongCallOption | Self::LongSharesOrBullCallSpread | Self::SellPutCreditSpreads
        )
    }

    pub const fn is_bearish(self) -> bool {
        matches!(
            self,
            Self::ProtectLongsOrBuyPuts | Self::SellCallCreditSpreads
        )
    }
}

/// Typed checklist factor with an exact point weight (`0..=10` scale before ×10 percentage scaling).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChecklistItem {
    PriceNearSupport,
    PriceApproachingSupport,
    BullishSentimentCrossoverPositiveSma,
    BullishSentimentCrossoverNegativeSma,
    BullishSentimentDivergence,
    UndervaluedRelativeToSector,
    FairlyValuedRelativeToSector,
}

impl ChecklistItem {
    pub const fn points(self) -> f64 {
        match self {
            Self::PriceNearSupport | Self::BullishSentimentCrossoverPositiveSma => 3.0,
            Self::PriceApproachingSupport | Self::BullishSentimentCrossoverNegativeSma => 1.5,
            Self::BullishSentimentDivergence | Self::UndervaluedRelativeToSector => 2.0,
            Self::FairlyValuedRelativeToSector => 1.0,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::PriceNearSupport => "Price near support level",
            Self::PriceApproachingSupport => "Price approaching support level",
            Self::BullishSentimentCrossoverPositiveSma => {
                "Bullish sentiment crossover (above positive SMA)"
            }
            Self::BullishSentimentCrossoverNegativeSma => {
                "Bullish sentiment crossover (above negative SMA)"
            }
            Self::BullishSentimentDivergence => {
                "Bullish sentiment divergence (price down, sentiment rising)"
            }
            Self::UndervaluedRelativeToSector => "Undervalued relative to sector benchmarks",
            Self::FairlyValuedRelativeToSector => "Fairly valued relative to sector benchmarks",
        }
    }
}

/// Ex-post outcome label for a historical buy signal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignalQuality {
    Pending,
    Good,
    Bad,
}

impl SignalQuality {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Good => "good",
            Self::Bad => "bad",
        }
    }
}

/// Pure scoring result mapped to `OpportunityResponse` by the API service.
#[derive(Debug, Clone, PartialEq)]
pub struct OpportunityResult {
    pub score: f64,
    pub class: OpportunityClass,
    pub strategy_kind: OpportunityStrategy,
    pub classification: &'static str,
    pub color: &'static str,
    pub strategy: &'static str,
    pub description: &'static str,
    pub items: Vec<ChecklistItem>,
}

impl OpportunityResult {
    /// Human-readable checklist explanations matching `self.items` 1-to-1.
    pub fn checklist_strings(&self) -> Vec<String> {
        self.items
            .iter()
            .map(|item| item.as_str().to_string())
            .collect()
    }
}

/// Pure Rule 1 classifier from signed distance `delta = p - support` and
/// `near_limit = support * SUPPORT_NEAR_BAND` (with `approach_limit = 2.0 * near_limit`).
#[inline]
pub fn classify_support_delta(delta: f64, near_limit: f64) -> Option<ChecklistItem> {
    let approach_limit = near_limit * 2.0;
    if !delta.is_finite()
        || !near_limit.is_finite()
        || near_limit < 0.0
        || !approach_limit.is_finite()
    {
        return None;
    }
    if delta >= -near_limit && delta <= near_limit {
        Some(ChecklistItem::PriceNearSupport)
    } else if delta > near_limit && delta <= approach_limit {
        Some(ChecklistItem::PriceApproachingSupport)
    } else {
        None
    }
}

/// Pure Rule 1 classifier for price proximity to support given a precomputed
/// `near_limit = support * SUPPORT_NEAR_BAND`.
#[inline]
pub fn classify_support_band(p: f64, support: f64, near_limit: f64) -> Option<ChecklistItem> {
    if !p.is_finite() || !support.is_finite() || support <= 0.0 || p <= 0.0 {
        return None;
    }
    classify_support_delta(p - support, near_limit)
}

/// Score a stock opportunity from price/support, sentiment momentum, divergence,
/// and sector-relative valuation.
///
/// - **Rule 1 (Bounded Support Band)**: Uses `rel = (price - support) / support`.
///   Prices within `[-2.5%, +2.5%]` of support receive `+3.0` (`PriceNearSupport`);
///   prices in `(2.5%, 5.0%]` above support receive `+1.5` (`PriceApproachingSupport`);
///   breakdowns strictly below `-2.5%` receive `+0.0` so falling knives do not
///   trigger a bullish support signal.
/// - **Rule 4 (Valuation)**: Accepts [`SectorRelativeScore`], enforcing at compile
///   time that `> 0.0` means undervalued relative to sector benchmarks.
#[allow(clippy::too_many_arguments)]
pub fn compute_opportunity(
    price: Option<f64>,
    support: f64,
    _resistance: f64,
    sentiment: f64,
    sentiment_sma: f64,
    vix_price: Option<f64>,
    pe_relative: Option<SectorRelativeScore>,
    prev_prices: &[f64],
    prev_sentiments: &[f64],
) -> OpportunityResult {
    let mut score: f64 = 0.0;
    let mut items = Vec::with_capacity(4);

    // 1. Price near support (bounded band [-2.5%, +2.5%], approach (2.5%, +5.0%])
    if let Some(p) = price {
        let near_limit = support * SUPPORT_NEAR_BAND;
        if let Some(item) = classify_support_band(p, support, near_limit) {
            score += item.points();
            items.push(item);
        }
    }

    // 2. Sentiment momentum (crossover)
    if sentiment.is_finite() && sentiment_sma.is_finite() && sentiment > sentiment_sma {
        let item = if sentiment_sma > 0.0 {
            ChecklistItem::BullishSentimentCrossoverPositiveSma
        } else {
            ChecklistItem::BullishSentimentCrossoverNegativeSma
        };
        score += item.points();
        items.push(item);
    }

    // 3. Sentiment-price divergence (price down, sentiment up in recent trailing window)
    if prev_prices.len() >= 3 && prev_sentiments.len() >= 3 {
        let first_p = prev_prices[0];
        let last_p = prev_prices[prev_prices.len() - 1];
        let first_s = prev_sentiments[0];
        let last_s = prev_sentiments[prev_sentiments.len() - 1];
        if first_p.is_finite() && last_p.is_finite() && first_s.is_finite() && last_s.is_finite() {
            let price_trend = last_p - first_p;
            let sentiment_trend = last_s - first_s;
            if price_trend < 0.0 && sentiment_trend > 0.1 {
                let item = ChecklistItem::BullishSentimentDivergence;
                score += item.points();
                items.push(item);
            }
        }
    }

    // 4. Valuation vs sector benchmark (positive SectorRelativeScore == undervalued)
    if let Some(pe_rel) = pe_relative {
        let v = pe_rel.value();
        if v.is_finite() {
            if v > 0.0 {
                let item = ChecklistItem::UndervaluedRelativeToSector;
                score += item.points();
                items.push(item);
            } else if v == 0.0 {
                let item = ChecklistItem::FairlyValuedRelativeToSector;
                score += item.points();
                items.push(item);
            }
        }
    }

    let opportunity_pct = (score * 10.0).clamp(0.0, 100.0);

    // Classification
    let class = if opportunity_pct >= 75.0 {
        OpportunityClass::StrongBuy
    } else if opportunity_pct >= 50.0 {
        OpportunityClass::Accumulate
    } else if opportunity_pct >= 30.0 {
        OpportunityClass::HoldNeutral
    } else {
        OpportunityClass::CautionOverbought
    };

    // Strategy Recommendation
    let vix = vix_price.filter(|v| v.is_finite()).unwrap_or(15.0);
    let strategy_kind = match class {
        OpportunityClass::StrongBuy | OpportunityClass::Accumulate => {
            if vix < 15.0 {
                OpportunityStrategy::LongCallOption
            } else if vix <= 22.0 {
                OpportunityStrategy::LongSharesOrBullCallSpread
            } else {
                OpportunityStrategy::SellPutCreditSpreads
            }
        }
        OpportunityClass::HoldNeutral => OpportunityStrategy::HoldOrSellCoveredCalls,
        OpportunityClass::CautionOverbought => {
            if vix > 22.0 {
                OpportunityStrategy::SellCallCreditSpreads
            } else {
                OpportunityStrategy::ProtectLongsOrBuyPuts
            }
        }
    };

    OpportunityResult {
        score: opportunity_pct,
        class,
        strategy_kind,
        classification: class.as_str(),
        color: class.color(),
        strategy: strategy_kind.as_str(),
        description: strategy_kind.description(),
        items,
    }
}

/// Evaluate ex-post quality of a buy signal against strictly forward prices using
/// an explicit target threshold.
pub fn evaluate_signal_quality_with_threshold(
    buy_signal: bool,
    curr_price: Option<f64>,
    threshold: f64,
    forward_prices: &[f64],
) -> SignalQuality {
    if !buy_signal {
        return SignalQuality::Pending;
    }
    if !matches!(curr_price, Some(p) if p.is_finite() && p > 0.0) {
        return SignalQuality::Pending;
    }

    let mut max_forward: Option<f64> = None;
    for &fp in forward_prices {
        if !fp.is_finite() {
            continue;
        }
        max_forward = Some(match max_forward {
            Some(prev) if prev >= fp => prev,
            _ => fp,
        });
    }

    let Some(best_forward) = max_forward else {
        return SignalQuality::Pending;
    };
    if !threshold.is_finite() {
        return SignalQuality::Bad;
    }
    if best_forward > threshold {
        SignalQuality::Good
    } else {
        SignalQuality::Bad
    }
}

/// Evaluate ex-post quality of a buy signal against strictly forward prices.
///
/// Returns `Pending` when `buy_signal` is false, `curr_price` is missing/non-positive/non-finite,
/// or no finite forward prices are available. Otherwise returns `Good` iff the maximum
/// finite forward price strictly exceeds `curr_price * SIGNAL_GOOD_RETURN_MULTIPLIER`.
pub fn evaluate_signal_quality(
    buy_signal: bool,
    curr_price: Option<f64>,
    forward_prices: &[f64],
) -> SignalQuality {
    let threshold = curr_price
        .filter(|p| p.is_finite() && *p > 0.0)
        .map_or(f64::NAN, |curr| curr * SIGNAL_GOOD_RETURN_MULTIPLIER);
    evaluate_signal_quality_with_threshold(buy_signal, curr_price, threshold, forward_prices)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn undervalued_pe_increases_opportunity_score() {
        let cheap_rel = SectorRelativeScore::lower_is_better(Some(10.0), Some(20.0));
        let expensive_rel = SectorRelativeScore::lower_is_better(Some(30.0), Some(20.0));
        assert!(cheap_rel.value() > 0.0);
        assert!(expensive_rel.value() < 0.0);

        let cheap_opp = compute_opportunity(
            Some(101.0),
            100.0,
            120.0,
            0.5,
            0.2,
            Some(14.0),
            Some(cheap_rel),
            &[105.0, 103.0, 101.0],
            &[0.1, 0.3, 0.5],
        );
        let expensive_opp = compute_opportunity(
            Some(101.0),
            100.0,
            120.0,
            0.5,
            0.2,
            Some(14.0),
            Some(expensive_rel),
            &[105.0, 103.0, 101.0],
            &[0.1, 0.3, 0.5],
        );

        assert_eq!(cheap_opp.score, 100.0);
        assert_eq!(expensive_opp.score, 80.0);
        assert!(cheap_opp.score > expensive_opp.score);
    }

    #[test]
    fn support_band_allows_minor_dip_but_rejects_breakdown() {
        // 1% below 5th-percentile support -> still near support (+30)
        let mild_dip =
            compute_opportunity(Some(99.0), 100.0, 120.0, 0.0, 0.0, None, None, &[], &[]);
        assert_eq!(mild_dip.score, 30.0);
        assert_eq!(mild_dip.items, vec![ChecklistItem::PriceNearSupport]);

        // 10% below support -> support broken (0)
        let breakdown =
            compute_opportunity(Some(90.0), 100.0, 120.0, 0.0, 0.0, None, None, &[], &[]);
        assert_eq!(breakdown.score, 0.0);
        assert!(breakdown.items.is_empty());

        // 4% above support -> approaching support (+15)
        let approach =
            compute_opportunity(Some(104.0), 100.0, 120.0, 0.0, 0.0, None, None, &[], &[]);
        assert_eq!(approach.score, 15.0);
        assert_eq!(approach.items, vec![ChecklistItem::PriceApproachingSupport]);
    }

    #[test]
    fn evaluates_signal_quality_cleanly() {
        assert_eq!(
            evaluate_signal_quality(false, Some(100.0), &[105.0]),
            SignalQuality::Pending
        );
        assert_eq!(
            evaluate_signal_quality(true, Some(100.0), &[]),
            SignalQuality::Pending
        );
        assert_eq!(
            evaluate_signal_quality(true, Some(100.0), &[100.1, 100.3]),
            SignalQuality::Good
        );
        assert_eq!(
            evaluate_signal_quality(true, Some(100.0), &[99.5, 100.2]),
            SignalQuality::Bad
        );
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_opportunity_score_bounds_accounting_and_strategy_matrix() {
        let price = if kani::any() { Some(kani::any()) } else { None };
        let support: f64 = kani::any();
        let resistance: f64 = kani::any();
        let sentiment: f64 = kani::any();
        let sentiment_sma: f64 = kani::any();
        let vix = if kani::any() { Some(kani::any()) } else { None };
        let pe_rel = if kani::any() {
            SectorRelativeScore::from_stored(kani::any())
        } else {
            None
        };
        let prev_prices: [f64; 3] = [kani::any(), kani::any(), kani::any()];
        let prev_sentiments: [f64; 3] = [kani::any(), kani::any(), kani::any()];

        let res = compute_opportunity(
            price,
            support,
            resistance,
            sentiment,
            sentiment_sma,
            vix,
            pe_rel,
            &prev_prices,
            &prev_sentiments,
        );

        // 1. Score bounds and checklist structural invariants.
        assert!(res.score.is_finite());
        assert!(res.score >= 0.0 && res.score <= 100.0);
        assert!(res.items.len() <= 4);

        // 2. Exact score <-> checklist accounting invariant.
        let mut item_points = 0.0;
        for &item in &res.items {
            item_points += item.points();
        }
        assert_eq!(res.score, item_points * 10.0);
        assert_eq!(res.score == 0.0, res.items.is_empty());

        // 3. Classification boundaries & buy-signal consistency.
        assert_eq!(res.class.is_buy_signal(), res.score >= 50.0);
        if res.score >= 75.0 {
            assert_eq!(res.class, OpportunityClass::StrongBuy);
        } else if res.score >= 50.0 {
            assert_eq!(res.class, OpportunityClass::Accumulate);
        } else if res.score >= 30.0 {
            assert_eq!(res.class, OpportunityClass::HoldNeutral);
        } else {
            assert_eq!(res.class, OpportunityClass::CautionOverbought);
        }

        // 4. Complete (OpportunityClass x VIX) -> OpportunityStrategy safety matrix.
        let eff_vix = vix.filter(|v| v.is_finite()).unwrap_or(15.0);
        match res.class {
            OpportunityClass::StrongBuy | OpportunityClass::Accumulate => {
                assert!(res.strategy_kind.is_bullish());
                assert!(!res.strategy_kind.is_bearish());
                if eff_vix < 15.0 {
                    assert_eq!(res.strategy_kind, OpportunityStrategy::LongCallOption);
                } else if eff_vix <= 22.0 {
                    assert_eq!(
                        res.strategy_kind,
                        OpportunityStrategy::LongSharesOrBullCallSpread
                    );
                } else {
                    assert_eq!(res.strategy_kind, OpportunityStrategy::SellPutCreditSpreads);
                }
            }
            OpportunityClass::HoldNeutral => {
                assert_eq!(
                    res.strategy_kind,
                    OpportunityStrategy::HoldOrSellCoveredCalls
                );
                assert!(!res.strategy_kind.is_bullish() && !res.strategy_kind.is_bearish());
            }
            OpportunityClass::CautionOverbought => {
                assert!(res.strategy_kind.is_bearish());
                assert!(!res.strategy_kind.is_bullish());
                if eff_vix > 22.0 {
                    assert_eq!(
                        res.strategy_kind,
                        OpportunityStrategy::SellCallCreditSpreads
                    );
                } else {
                    assert_eq!(
                        res.strategy_kind,
                        OpportunityStrategy::ProtectLongsOrBuyPuts
                    );
                }
            }
        }
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_all_classes_and_strategies_are_reachable() {
        // Discrete input grid covering each rule branch to prove zero dead branches
        // across all 4 OpportunityClass and all 6 OpportunityStrategy variants.
        let price = if kani::any() {
            Some(100.0)
        } else {
            Some(120.0)
        };
        let sentiment = if kani::any() { 0.5 } else { -0.5 };
        let sentiment_sma = if kani::any() { 0.2 } else { 0.0 };
        let vix = match kani::any::<u8>() % 3 {
            0 => Some(12.0),
            1 => Some(18.0),
            _ => Some(28.0),
        };
        let pe_rel = if kani::any() {
            SectorRelativeScore::from_stored(0.5)
        } else {
            None
        };
        let prev_prices = if kani::any() {
            [105.0, 102.0, 100.0]
        } else {
            [100.0, 102.0, 105.0]
        };
        let prev_sentiments = [0.0, 0.2, 0.5];

        let res = compute_opportunity(
            price,
            100.0,
            120.0,
            sentiment,
            sentiment_sma,
            vix,
            pe_rel,
            &prev_prices,
            &prev_sentiments,
        );

        kani::cover!(res.class == OpportunityClass::StrongBuy);
        kani::cover!(res.class == OpportunityClass::Accumulate);
        kani::cover!(res.class == OpportunityClass::HoldNeutral);
        kani::cover!(res.class == OpportunityClass::CautionOverbought);

        kani::cover!(res.strategy_kind == OpportunityStrategy::LongCallOption);
        kani::cover!(res.strategy_kind == OpportunityStrategy::LongSharesOrBullCallSpread);
        kani::cover!(res.strategy_kind == OpportunityStrategy::SellPutCreditSpreads);
        kani::cover!(res.strategy_kind == OpportunityStrategy::HoldOrSellCoveredCalls);
        kani::cover!(res.strategy_kind == OpportunityStrategy::ProtectLongsOrBuyPuts);
        kani::cover!(res.strategy_kind == OpportunityStrategy::SellCallCreditSpreads);
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_end_to_end_pe_monotonicity() {
        let sector_pe: f64 = kani::any();
        let pe_cheap: f64 = kani::any();
        let pe_expensive: f64 = kani::any();

        kani::assume(sector_pe.is_finite() && sector_pe > 1e-3 && sector_pe < 1e4);
        kani::assume(pe_cheap.is_finite() && pe_cheap > 0.0 && pe_cheap < 1e4);
        kani::assume(pe_expensive.is_finite() && pe_expensive > 0.0 && pe_expensive < 1e4);
        kani::assume(pe_cheap <= pe_expensive);

        let rel_cheap = SectorRelativeScore::lower_is_better(Some(pe_cheap), Some(sector_pe));
        let rel_expensive =
            SectorRelativeScore::lower_is_better(Some(pe_expensive), Some(sector_pe));

        let sentiment: f64 = kani::any();
        let sentiment_sma: f64 = kani::any();

        let opp_cheap = compute_opportunity(
            Some(100.0),
            99.0,
            120.0,
            sentiment,
            sentiment_sma,
            Some(15.0),
            Some(rel_cheap),
            &[],
            &[],
        );
        let opp_expensive = compute_opportunity(
            Some(100.0),
            99.0,
            120.0,
            sentiment,
            sentiment_sma,
            Some(15.0),
            Some(rel_expensive),
            &[],
            &[],
        );

        assert!(opp_cheap.score >= opp_expensive.score);
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_sentiment_monotonicity() {
        let s1: f64 = kani::any();
        let s2: f64 = kani::any();
        let sma: f64 = kani::any();
        kani::assume(s1.is_finite() && s2.is_finite() && sma.is_finite());
        kani::assume(s1 <= s2);

        let opp1 = compute_opportunity(None, 0.0, 0.0, s1, sma, None, None, &[], &[]);
        let opp2 = compute_opportunity(None, 0.0, 0.0, s2, sma, None, None, &[], &[]);
        assert!(opp2.score >= opp1.score);
    }

    #[kani::proof]
    fn verify_price_support_above_monotonicity() {
        let near_limit: f64 = kani::any();
        let delta_near: f64 = kani::any();
        let delta_far: f64 = kani::any();

        kani::assume(near_limit.is_finite() && near_limit >= 0.0 && near_limit < 1e5);
        kani::assume(delta_near.is_finite() && delta_far.is_finite());
        // Above support (delta >= 0), moving closer to support (smaller delta)
        // never lowers the Rule 1 score. By IEEE-754 subtraction monotonicity,
        // `support <= p_near <= p_far ==> 0 <= p_near - support <= p_far - support`.
        kani::assume(0.0 <= delta_near && delta_near <= delta_far);

        let pts_near =
            classify_support_delta(delta_near, near_limit).map_or(0.0, ChecklistItem::points);
        let pts_far =
            classify_support_delta(delta_far, near_limit).map_or(0.0, ChecklistItem::points);
        assert!(pts_near >= pts_far);
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_price_support_breakdown_protection() {
        let support: f64 = kani::any();
        let p_broken: f64 = kani::any();

        kani::assume(support.is_finite() && support > 1e-3 && support < 1e5);
        // Severe breakdown (> 2.5% below support) must never earn a support bonus.
        kani::assume(p_broken.is_finite() && p_broken > 0.0 && p_broken < support * 0.974);

        let opp_broken =
            compute_opportunity(Some(p_broken), support, 0.0, 0.0, 0.0, None, None, &[], &[]);
        assert_eq!(opp_broken.score, 0.0);
        assert!(opp_broken.items.is_empty());
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_divergence_rule_monotonicity_and_nan_safety() {
        let p_start: f64 = kani::any();
        let p_end_strong: f64 = kani::any();
        let p_end_weak: f64 = kani::any();
        let s_start: f64 = kani::any();
        let s_end_strong: f64 = kani::any();
        let s_end_weak: f64 = kani::any();

        // Non-finite history entry must fail closed (0 points).
        let opp_nan = compute_opportunity(
            None,
            0.0,
            0.0,
            0.0,
            0.0,
            None,
            None,
            &[100.0, 99.0, f64::NAN],
            &[0.0, 0.2, 0.5],
        );
        assert_eq!(opp_nan.score, 0.0);

        // Stronger price drop (p_end_strong <= p_end_weak) and larger sentiment rise
        // (s_end_strong >= s_end_weak) must never produce a lower score.
        kani::assume(p_start.is_finite() && p_start.abs() < 1e6);
        kani::assume(p_end_strong.is_finite() && p_end_strong.abs() < 1e6);
        kani::assume(p_end_weak.is_finite() && p_end_weak.abs() < 1e6);
        kani::assume(s_start.is_finite() && s_start.abs() < 1e6);
        kani::assume(s_end_strong.is_finite() && s_end_strong.abs() < 1e6);
        kani::assume(s_end_weak.is_finite() && s_end_weak.abs() < 1e6);
        kani::assume(p_end_strong <= p_end_weak);
        kani::assume(s_end_strong >= s_end_weak);

        let opp_strong = compute_opportunity(
            None,
            0.0,
            0.0,
            0.0,
            0.0,
            None,
            None,
            &[p_start, 0.0, p_end_strong],
            &[s_start, 0.0, s_end_strong],
        );
        let opp_weak = compute_opportunity(
            None,
            0.0,
            0.0,
            0.0,
            0.0,
            None,
            None,
            &[p_start, 0.0, p_end_weak],
            &[s_start, 0.0, s_end_weak],
        );
        assert!(opp_strong.score >= opp_weak.score);
    }

    #[kani::proof]
    #[kani::unwind(4)]
    fn verify_evaluate_signal_quality_safety() {
        let buy_signal: bool = kani::any();
        let curr_price = if kani::any() { Some(kani::any()) } else { None };
        let fp1: f64 = kani::any();
        let fp2: f64 = kani::any();

        let q = evaluate_signal_quality(buy_signal, curr_price, &[fp1, fp2]);

        let valid_curr = matches!(curr_price, Some(p) if p.is_finite() && p > 0.0);
        let any_finite_fp = fp1.is_finite() || fp2.is_finite();
        if !buy_signal || !valid_curr || !any_finite_fp {
            assert_eq!(q, SignalQuality::Pending);
            return;
        }

        let curr = curr_price.unwrap();
        if q == SignalQuality::Good {
            assert!(
                (fp1.is_finite() && fp1 > curr) || (fp2.is_finite() && fp2 > curr),
                "Good signal must have a forward price strictly above entry"
            );
        }
    }

    #[kani::proof]
    #[kani::unwind(4)]
    fn verify_evaluate_signal_quality_monotonicity() {
        let curr: f64 = kani::any();
        let threshold: f64 = kani::any();
        let fp1: f64 = kani::any();
        let fp2: f64 = kani::any();
        let fp2_higher: f64 = kani::any();

        kani::assume(curr.is_finite() && curr > 0.0);
        kani::assume(fp2.is_finite() && fp2_higher.is_finite() && fp2_higher >= fp2);

        let q = evaluate_signal_quality_with_threshold(true, Some(curr), threshold, &[fp1, fp2]);
        let q_higher =
            evaluate_signal_quality_with_threshold(true, Some(curr), threshold, &[fp1, fp2_higher]);
        if q == SignalQuality::Good {
            assert_eq!(q_higher, SignalQuality::Good);
        }
    }
}
