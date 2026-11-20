//! Opportunity scoring and options strategy classification.

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
}

/// Pure scoring result mapped to `OpportunityResponse` by the API service.
#[derive(Debug, Clone, PartialEq)]
pub struct OpportunityResult {
    pub score: f64,
    pub class: OpportunityClass,
    pub classification: &'static str,
    pub color: &'static str,
    pub strategy: &'static str,
    pub description: &'static str,
    pub checklist: Vec<&'static str>,
}

/// Score a stock opportunity from price/support, sentiment momentum, divergence,
/// and sector-relative valuation.
///
/// Sign convention (matching `market::calculate_relative(..., invert = true)` and
/// the UI scorecard): `pe_relative > 0.0` means **undervalued** relative to the
/// sector benchmark (stock P/E < sector P/E).
#[allow(clippy::too_many_arguments)]
pub fn compute_opportunity(
    price: Option<f64>,
    support: f64,
    _resistance: f64,
    sentiment: f64,
    sentiment_sma: f64,
    vix_price: Option<f64>,
    pe_relative: Option<f64>,
    prev_prices: &[f64],
    prev_sentiments: &[f64],
) -> OpportunityResult {
    let mut score: f64 = 0.0;
    let mut checklist = Vec::with_capacity(4);

    // 1. Price near support
    if let Some(p) = price {
        if p.is_finite() && support.is_finite() && support > 0.0 && p > 0.0 {
            let dist_to_support = (p - support) / p;
            if dist_to_support <= 0.025 {
                score += 3.0;
                checklist.push("Price near support level");
            } else if dist_to_support <= 0.05 {
                score += 1.5;
                checklist.push("Price approaching support level");
            }
        }
    }

    // 2. Sentiment momentum (crossover)
    if sentiment.is_finite() && sentiment_sma.is_finite() && sentiment > sentiment_sma {
        if sentiment_sma > 0.0 {
            score += 3.0;
            checklist.push("Bullish sentiment crossover (above positive SMA)");
        } else {
            score += 1.5;
            checklist.push("Bullish sentiment crossover (above negative SMA)");
        }
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
                score += 2.0;
                checklist.push("Bullish sentiment divergence (price down, sentiment rising)");
            }
        }
    }

    // 4. Valuation vs sector benchmark (positive pe_relative == lower P/E == undervalued)
    if let Some(pe_rel) = pe_relative {
        if pe_rel.is_finite() {
            if pe_rel > 0.0 {
                score += 2.0;
                checklist.push("Undervalued relative to sector benchmarks");
            } else if pe_rel == 0.0 {
                score += 1.0;
                checklist.push("Fairly valued relative to sector benchmarks");
            }
        }
    }

    let opportunity_pct = (score / 10.0 * 100.0).clamp(0.0, 100.0);

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
    let mut strategy = "Hold / Sell Covered Calls";
    let mut strategy_desc =
        "Neutral market stance. Capitalize on range-bound behavior using covered call writing.";

    let vix = vix_price.filter(|v| v.is_finite()).unwrap_or(15.0);

    match class {
        OpportunityClass::StrongBuy | OpportunityClass::Accumulate => {
            if vix < 15.0 {
                strategy = "Long Call Option (Buy Call)";
                strategy_desc =
                    "Low volatility regime. Buy outright call options to leverage cheap premium.";
            } else if vix <= 22.0 {
                strategy = "Long Shares / Bull Call Spread";
                strategy_desc =
                    "Moderate volatility. Buy shares directly or use bull call debit spreads.";
            } else {
                strategy = "Sell Put Credit Spreads";
                strategy_desc =
                    "High volatility regime. Sell put credit spreads to harvest rich premium.";
            }
        }
        OpportunityClass::CautionOverbought => {
            if vix > 22.0 {
                strategy = "Sell Call Credit Spreads";
                strategy_desc =
                    "High volatility overbought. Sell call credit spreads for fast premium decay.";
            } else {
                strategy = "Protect Longs / Buy Puts";
                strategy_desc =
                    "Overbought stance. Trim shares or buy puts to hedge downside risk.";
            }
        }
        OpportunityClass::HoldNeutral => {}
    }

    OpportunityResult {
        score: opportunity_pct,
        class,
        classification: class.as_str(),
        color: class.color(),
        strategy,
        description: strategy_desc,
        checklist,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::market::calculate_relative;

    #[test]
    fn undervalued_pe_increases_opportunity_score() {
        let cheap_rel = calculate_relative(Some(10.0), Some(20.0), true);
        let expensive_rel = calculate_relative(Some(30.0), Some(20.0), true);
        assert!(cheap_rel > 0.0);
        assert!(expensive_rel < 0.0);

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
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;
    use crate::market::calculate_relative;

    #[kani::proof]
    fn verify_opportunity_score_bounds_and_strategy_consistency() {
        let price = if kani::any() { Some(kani::any()) } else { None };
        let support: f64 = kani::any();
        let resistance: f64 = kani::any();
        let sentiment: f64 = kani::any();
        let sentiment_sma: f64 = kani::any();
        let vix = if kani::any() { Some(kani::any()) } else { None };
        let pe_rel = if kani::any() { Some(kani::any()) } else { None };
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

        assert!(res.score.is_finite());
        assert!(res.score >= 0.0 && res.score <= 100.0);
        assert!(res.checklist.len() <= 4);

        // Verify classification boundaries and strategy totality.
        if res.score >= 75.0 {
            assert_eq!(res.class, OpportunityClass::StrongBuy);
        } else if res.score >= 50.0 {
            assert_eq!(res.class, OpportunityClass::Accumulate);
        } else if res.score >= 30.0 {
            assert_eq!(res.class, OpportunityClass::HoldNeutral);
        } else {
            assert_eq!(res.class, OpportunityClass::CautionOverbought);
        }
    }

    #[kani::proof]
    fn verify_end_to_end_pe_monotonicity() {
        let sector_pe: f64 = kani::any();
        let pe_cheap: f64 = kani::any();
        let pe_expensive: f64 = kani::any();

        kani::assume(sector_pe.is_finite() && sector_pe > 1e-3 && sector_pe < 1e4);
        kani::assume(pe_cheap.is_finite() && pe_cheap > 0.0 && pe_cheap < 1e4);
        kani::assume(pe_expensive.is_finite() && pe_expensive > 0.0 && pe_expensive < 1e4);
        kani::assume(pe_cheap <= pe_expensive);

        let rel_cheap = calculate_relative(Some(pe_cheap), Some(sector_pe), true);
        let rel_expensive = calculate_relative(Some(pe_expensive), Some(sector_pe), true);

        let sentiment: f64 = kani::any();
        let sentiment_sma: f64 = kani::any();
        let price = Some(100.0);
        let support = 99.0;

        let opp_cheap = compute_opportunity(
            price,
            support,
            120.0,
            sentiment,
            sentiment_sma,
            Some(15.0),
            Some(rel_cheap),
            &[],
            &[],
        );
        let opp_expensive = compute_opportunity(
            price,
            support,
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
}
