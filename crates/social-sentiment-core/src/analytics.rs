//! Session-aware analytics shared by the API and ingestion services.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DailyClose {
    pub ends_at: DateTime<Utc>,
    pub close_price: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LagStatistic {
    pub lag_sessions: usize,
    pub correlation: Option<f64>,
    pub beta: Option<f64>,
    pub sample_count: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RelationshipStatistic {
    pub correlation: Option<f64>,
    pub beta: Option<f64>,
    pub selected_lag: Option<usize>,
    pub sample_count: usize,
    pub strength: Option<&'static str>,
    pub lag_statistics: Vec<LagStatistic>,
}

pub fn correlation_strength(correlation: Option<f64>) -> Option<&'static str> {
    let absolute = correlation?.abs();
    Some(if absolute < 0.20 {
        "weak"
    } else if absolute < 0.40 {
        "moderate"
    } else {
        "strong"
    })
}

fn returns(closes: &[DailyClose]) -> Vec<(DateTime<Utc>, f64)> {
    closes
        .windows(2)
        .filter_map(|pair| {
            let previous = pair[0];
            let current = pair[1];
            (previous.close_price > 0.0 && current.close_price > 0.0).then_some((
                current.ends_at,
                current.close_price / previous.close_price - 1.0,
            ))
        })
        .collect()
}

fn pearson_and_beta(pairs: &[(f64, f64)], minimum_samples: usize) -> (Option<f64>, Option<f64>) {
    if pairs.len() < minimum_samples {
        return (None, None);
    }

    let count = pairs.len() as f64;
    let factor_mean = pairs.iter().map(|(factor, _)| factor).sum::<f64>() / count;
    let stock_mean = pairs.iter().map(|(_, stock)| stock).sum::<f64>() / count;
    let factor_ss = pairs
        .iter()
        .map(|(factor, _)| (factor - factor_mean).powi(2))
        .sum::<f64>();
    let stock_ss = pairs
        .iter()
        .map(|(_, stock)| (stock - stock_mean).powi(2))
        .sum::<f64>();
    if factor_ss <= 0.0 || stock_ss <= 0.0 {
        return (None, None);
    }

    let covariance = pairs
        .iter()
        .map(|(factor, stock)| (factor - factor_mean) * (stock - stock_mean))
        .sum::<f64>();
    (
        Some(covariance / (factor_ss * stock_ss).sqrt()),
        Some(covariance / factor_ss),
    )
}

/// Align factor returns to the first later U.S. close and score each lag.
pub fn calculate_relationship(
    factor_closes: &[DailyClose],
    stock_closes: &[DailyClose],
    horizon_sessions: usize,
) -> RelationshipStatistic {
    calculate_relationship_with_options(
        factor_closes,
        stock_closes,
        horizon_sessions,
        &[0, 1, 2],
        20,
    )
}

pub fn calculate_relationship_with_options(
    factor_closes: &[DailyClose],
    stock_closes: &[DailyClose],
    horizon_sessions: usize,
    lags: &[usize],
    minimum_samples: usize,
) -> RelationshipStatistic {
    assert!(horizon_sessions > 0, "horizon_sessions must be positive");
    assert!(minimum_samples > 1, "minimum_samples must exceed one");
    assert!(!lags.is_empty(), "lags must not be empty");

    let mut factors = factor_closes.to_vec();
    let mut stocks = stock_closes.to_vec();
    factors.sort_by_key(|close| close.ends_at);
    stocks.sort_by_key(|close| close.ends_at);
    let factor_returns = returns(&factors);
    let stock_returns = returns(&stocks);

    let lag_statistics = lags
        .iter()
        .map(|&lag| {
            let mut pairs = factor_returns
                .iter()
                .filter_map(|(factor_end, factor_return)| {
                    let first_later =
                        stock_returns.partition_point(|(stock_end, _)| stock_end <= factor_end);
                    stock_returns
                        .get(first_later + lag)
                        .map(|(_, stock_return)| (*factor_return, *stock_return))
                })
                .collect::<Vec<_>>();
            if pairs.len() > horizon_sessions {
                pairs.drain(..pairs.len() - horizon_sessions);
            }
            let (correlation, beta) = pearson_and_beta(&pairs, minimum_samples);
            LagStatistic {
                lag_sessions: lag,
                correlation,
                beta,
                sample_count: pairs.len(),
            }
        })
        .collect::<Vec<_>>();

    let selected = lag_statistics
        .iter()
        .filter(|statistic| statistic.correlation.is_some())
        .max_by(|left, right| {
            left.correlation
                .expect("filtered")
                .abs()
                .total_cmp(&right.correlation.expect("filtered").abs())
                .then_with(|| right.lag_sessions.cmp(&left.lag_sessions))
        });

    if let Some(selected) = selected {
        RelationshipStatistic {
            correlation: selected.correlation,
            beta: selected.beta,
            selected_lag: Some(selected.lag_sessions),
            sample_count: selected.sample_count,
            strength: correlation_strength(selected.correlation),
            lag_statistics,
        }
    } else {
        RelationshipStatistic {
            correlation: None,
            beta: None,
            selected_lag: None,
            sample_count: lag_statistics
                .iter()
                .map(|statistic| statistic.sample_count)
                .max()
                .unwrap_or(0),
            strength: None,
            lag_statistics,
        }
    }
}

/// Percent move from the last pre-event close to the first post-event close.
pub fn next_close_move(stock_closes: &[DailyClose], occurred_at: DateTime<Utc>) -> Option<f64> {
    let mut closes = stock_closes.to_vec();
    closes.sort_by_key(|close| close.ends_at);
    let before_index = closes.partition_point(|close| close.ends_at < occurred_at);
    let after_index = closes.partition_point(|close| close.ends_at <= occurred_at);
    if before_index == 0 || after_index >= closes.len() {
        return None;
    }
    let before = closes[before_index - 1].close_price;
    let after = closes[after_index].close_price;
    (before > 0.0 && after > 0.0).then_some((after / before - 1.0) * 100.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn close(day: u32, hour: u32, price: f64) -> DailyClose {
        DailyClose {
            ends_at: Utc.with_ymd_and_hms(2026, 1, day, hour, 0, 0).unwrap(),
            close_price: price,
        }
    }

    #[test]
    fn relationship_aligns_to_first_later_close() {
        let factor_returns = [0.011, -0.006, 0.018, 0.004, -0.014];
        let mut factor_price = 100.0;
        let mut stock_price = 100.0;
        let mut factors = vec![close(1, 8, factor_price)];
        let mut stocks = vec![close(1, 21, stock_price)];
        for index in 0..25 {
            let daily_return = factor_returns[index % factor_returns.len()];
            factor_price *= 1.0 + daily_return;
            stock_price *= 1.0 + daily_return;
            let day = (index + 2) as u32;
            factors.push(close(day, 8, factor_price));
            stocks.push(close(day, 21, stock_price));
        }
        let statistic = calculate_relationship(&factors, &stocks, 30);
        assert_eq!(statistic.selected_lag, Some(0));
        assert_eq!(statistic.sample_count, 25);
        assert!(statistic.correlation.expect("correlation") > 0.999_999);
    }

    #[test]
    fn next_close_reaction_uses_strictly_adjacent_event_closes() {
        let closes = [close(1, 21, 100.0), close(2, 21, 105.0)];
        let event = Utc.with_ymd_and_hms(2026, 1, 2, 12, 0, 0).unwrap();
        let reaction = next_close_move(&closes, event).expect("reaction");
        assert!((reaction - 5.0).abs() < 1e-12);
    }
}
