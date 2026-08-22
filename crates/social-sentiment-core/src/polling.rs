//! Shared request pacing and provider outcome classification.

use std::{collections::HashMap, time::Duration};

use tokio::{sync::Mutex, time::Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PollStatus {
    Success,
    NoData,
    RateLimited,
    Blocked,
    TransientError,
    Error,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PollOutcome {
    pub status: PollStatus,
    pub retry_after: Option<Duration>,
}

impl PollOutcome {
    pub fn from_http(status: u16, retry_after: Option<&str>) -> Self {
        match status {
            200..=299 => Self {
                status: PollStatus::Success,
                retry_after: None,
            },
            403 => Self {
                status: PollStatus::Blocked,
                retry_after: None,
            },
            429 => Self {
                status: PollStatus::RateLimited,
                retry_after: retry_after
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|seconds| *seconds > 0)
                    .map(Duration::from_secs),
            },
            500..=599 => Self {
                status: PollStatus::TransientError,
                retry_after: None,
            },
            _ => Self {
                status: PollStatus::Error,
                retry_after: None,
            },
        }
    }
}

#[derive(Debug)]
struct Bucket {
    tokens: f64,
    last_refill: Instant,
}

/// Continuously refilled token bucket which spreads starts across a period.
#[derive(Debug)]
pub struct RateLimiter {
    max_rate: f64,
    period: Duration,
    bucket: Mutex<Bucket>,
}

impl RateLimiter {
    pub fn new(max_rate: f64, period: Duration) -> Self {
        let max_rate = max_rate.max(f64::EPSILON);
        Self {
            max_rate,
            period: period.max(Duration::from_nanos(1)),
            bucket: Mutex::new(Bucket {
                tokens: max_rate,
                last_refill: Instant::now(),
            }),
        }
    }

    pub async fn acquire(&self) {
        let mut bucket = self.bucket.lock().await;
        loop {
            let now = Instant::now();
            let refill = now.duration_since(bucket.last_refill).as_secs_f64()
                * (self.max_rate / self.period.as_secs_f64());
            bucket.tokens = self.max_rate.min(bucket.tokens + refill);
            bucket.last_refill = now;
            if bucket.tokens >= 1.0 {
                bucket.tokens -= 1.0;
                return;
            }
            let wait = Duration::from_secs_f64(
                (1.0 - bucket.tokens) * (self.period.as_secs_f64() / self.max_rate),
            );
            tokio::time::sleep(wait).await;
        }
    }
}

/// Independent exponential cooldowns prevent one symbol from stalling a batch.
#[derive(Debug)]
pub struct PerSymbolBackoff {
    base: Duration,
    maximum: Duration,
    penalties: HashMap<String, Duration>,
    next_allowed: HashMap<String, Instant>,
}

impl PerSymbolBackoff {
    pub fn new(base: Duration, maximum: Duration) -> Self {
        Self {
            base: base.max(Duration::from_nanos(1)),
            maximum: maximum.max(base),
            penalties: HashMap::new(),
            next_allowed: HashMap::new(),
        }
    }

    pub fn is_due(&self, symbol: &str, now: Instant) -> bool {
        self.next_allowed
            .get(symbol)
            .is_none_or(|next| now >= *next)
    }

    pub fn record(&mut self, symbol: &str, rate_limited: bool, now: Instant) {
        self.record_with_retry_after(symbol, rate_limited, None, now);
    }

    pub fn record_with_retry_after(
        &mut self,
        symbol: &str,
        rate_limited: bool,
        retry_after: Option<Duration>,
        now: Instant,
    ) {
        if !rate_limited {
            self.penalties.remove(symbol);
            self.next_allowed.remove(symbol);
            return;
        }
        let next_penalty = retry_after.unwrap_or_else(|| {
            self.penalties
                .get(symbol)
                .and_then(|current| current.checked_mul(2))
                .unwrap_or(self.base)
        });
        let next_penalty = next_penalty.max(Duration::from_nanos(1)).min(self.maximum);
        self.penalties.insert(symbol.to_string(), next_penalty);
        self.next_allowed
            .insert(symbol.to_string(), now + next_penalty);
    }

    pub fn penalized(&self) -> Vec<&str> {
        let mut symbols = self
            .penalties
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        symbols.sort_unstable();
        symbols
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_provider_http_failures_and_retry_after() {
        assert_eq!(
            PollOutcome::from_http(429, Some("120")),
            PollOutcome {
                status: PollStatus::RateLimited,
                retry_after: Some(Duration::from_secs(120)),
            }
        );
        assert_eq!(
            PollOutcome::from_http(403, None).status,
            PollStatus::Blocked
        );
        assert_eq!(
            PollOutcome::from_http(503, None).status,
            PollStatus::TransientError
        );
    }

    #[tokio::test(start_paused = true)]
    async fn backoff_is_per_symbol_and_resets_after_success() {
        let now = Instant::now();
        let mut backoff = PerSymbolBackoff::new(Duration::from_secs(10), Duration::from_secs(40));
        backoff.record("NVDA", true, now);
        assert!(!backoff.is_due("NVDA", now));
        assert!(backoff.is_due("AAPL", now));
        backoff.record("NVDA", false, now);
        assert!(backoff.is_due("NVDA", now));
    }

    #[tokio::test(start_paused = true)]
    async fn retry_after_applies_only_to_the_affected_key() {
        let now = Instant::now();
        let mut backoff = PerSymbolBackoff::new(Duration::from_secs(10), Duration::from_secs(60));
        backoff.record_with_retry_after("NVDA", true, Some(Duration::from_secs(45)), now);
        assert!(!backoff.is_due("NVDA", now + Duration::from_secs(44)));
        assert!(backoff.is_due("NVDA", now + Duration::from_secs(45)));
        assert!(backoff.is_due("AAPL", now));
    }
}
