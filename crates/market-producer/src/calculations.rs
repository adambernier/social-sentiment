use std::collections::HashMap;

use crate::DailyClose;
use social_sentiment_core::schemas::StockMetrics;

pub fn calculate_relative(value: Option<f64>, baseline: Option<f64>, invert: bool) -> f64 {
    match (value, baseline) {
        (Some(value), Some(baseline)) if baseline != 0.0 => {
            let relative = (value - baseline) / baseline;
            if invert {
                -relative
            } else {
                relative
            }
        }
        _ => 0.0,
    }
}

pub fn one_year_return(history: &[DailyClose]) -> Option<f64> {
    let first = history.first()?.close;
    let last = history.last()?.close;
    (first.is_finite() && last.is_finite() && first > 0.0).then_some((last - first) / first)
}

pub fn beta(stock: &[DailyClose], baseline: &[DailyClose]) -> f64 {
    let returns = |history: &[DailyClose]| {
        history
            .windows(2)
            .filter_map(|pair| {
                let previous = pair[0].close;
                (previous > 0.0).then_some((pair[1].date, pair[1].close / previous - 1.0))
            })
            .collect::<HashMap<_, _>>()
    };
    let stock_returns = returns(stock);
    let baseline_returns = returns(baseline);
    let pairs = stock_returns
        .iter()
        .filter_map(|(date, stock_return)| {
            baseline_returns
                .get(date)
                .map(|base| (*stock_return, *base))
        })
        .collect::<Vec<_>>();
    if pairs.len() <= 30 {
        return 1.0;
    }
    let stock_mean = pairs.iter().map(|pair| pair.0).sum::<f64>() / pairs.len() as f64;
    let baseline_mean = pairs.iter().map(|pair| pair.1).sum::<f64>() / pairs.len() as f64;
    let covariance = pairs
        .iter()
        .map(|pair| (pair.0 - stock_mean) * (pair.1 - baseline_mean))
        .sum::<f64>()
        / (pairs.len() - 1) as f64;
    let variance = pairs
        .iter()
        .map(|pair| (pair.1 - baseline_mean).powi(2))
        .sum::<f64>()
        / (pairs.len() - 1) as f64;
    if variance == 0.0 {
        1.0
    } else {
        covariance / variance
    }
}

pub fn build_metrics(
    symbol: &str,
    pe: Option<f64>,
    history: &[DailyClose],
    baseline_pe: Option<f64>,
    baseline_history: &[DailyClose],
) -> Option<StockMetrics> {
    let average_return = one_year_return(history)?;
    let baseline_return = one_year_return(baseline_history);
    let beta = beta(history, baseline_history);
    Some(StockMetrics {
        symbol: symbol.to_owned(),
        pe_ratio: pe,
        beta: Some(beta),
        avg_return_1y: Some(average_return),
        inflation_adj_return_1y: Some(average_return - 0.03),
        pe_relative_sector: Some(calculate_relative(pe, baseline_pe, true)),
        beta_relative_sector: Some(calculate_relative(Some(beta), Some(1.0), true)),
        return_relative_sector: Some(calculate_relative(
            Some(average_return),
            baseline_return,
            false,
        )),
    })
}

/// Normalize all FX bars as local-currency units per USD. Inverting swaps high
/// and low so the OHLC invariant remains valid.
pub fn normalize_fx_ohlc(
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    provider_is_local_per_usd: bool,
) -> anyhow::Result<(f64, f64, f64, f64)> {
    if provider_is_local_per_usd {
        return Ok((open, high, low, close));
    }
    if [open, high, low, close].iter().any(|price| *price <= 0.0) {
        anyhow::bail!("FX prices must be positive before inversion");
    }
    Ok((1.0 / open, 1.0 / low, 1.0 / high, 1.0 / close))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inversion_preserves_high_low_order() {
        assert_eq!(
            normalize_fx_ohlc(0.5, 0.8, 0.4, 0.625, false).unwrap(),
            (2.0, 2.5, 1.25, 1.6)
        );
    }

    #[test]
    fn relative_handles_missing_and_zero_baseline() {
        assert_eq!(calculate_relative(Some(2.0), Some(1.0), false), 1.0);
        assert_eq!(calculate_relative(Some(2.0), Some(1.0), true), -1.0);
        assert_eq!(calculate_relative(Some(2.0), Some(0.0), false), 0.0);
    }
}
