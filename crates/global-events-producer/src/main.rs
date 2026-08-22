use std::{env, time::Duration};

use anyhow::Result;
use chrono::{TimeDelta, Utc};
use global_events_producer::{
    load_rules, store_rule_events, GdeltEventAdapter, GlobalEventAdapter,
};
use social_sentiment_core::{
    config::Config,
    observability::{
        increment_global_provider_request, increment_global_rule_matches,
        increment_provider_request, increment_rate_limit, initialize_rate_limit,
        set_global_last_success, start_metrics_server,
    },
    polling::RateLimiter,
    producer::ProviderStatus,
    runtime::shutdown_signal,
};
use sqlx::postgres::PgPoolOptions;
use tracing::{error, info, warn};

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let config = Config::from_env();
    initialize_rate_limit("global-events");
    let _metrics = start_metrics_server(8010, "global-events");
    if !config.global_context_enabled {
        info!("global context is disabled; event ingestion is idle");
        shutdown_signal().await;
        return Ok(());
    }
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect(&config.database_dsn)
        .await?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .connect_timeout(Duration::from_secs(10))
        .user_agent("social-sentiment-global-context/1.0")
        .build()?;
    let adapter = GdeltEventAdapter::new(client);
    let limiter = RateLimiter::new(
        env_u64("GLOBAL_EVENTS_RATE_PER_MINUTE", 10) as f64,
        Duration::from_secs(60),
    );
    let interval = Duration::from_secs(env_u64("GLOBAL_EVENTS_POLL_SECONDS", 900));
    let lookback = i64::try_from(env_u64("GLOBAL_EVENTS_LOOKBACK_HOURS", 48)).unwrap_or(48);
    let cap = i64::try_from(env_u64("GLOBAL_EVENTS_MAX_PER_RULE_DAY", 20)).unwrap_or(20);
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    loop {
        let cycle = poll_once(&pool, &adapter, &limiter, lookback, cap);
        tokio::select! {
            result = cycle => {
                if let Err(error) = result {
                    error!(%error, "global event poll cycle failed; retrying next interval");
                }
            }
            () = &mut shutdown => break,
        }
        tokio::select! {
            () = tokio::time::sleep(interval) => {},
            () = &mut shutdown => break,
        }
    }
    Ok(())
}

async fn poll_once(
    pool: &sqlx::PgPool,
    adapter: &dyn GlobalEventAdapter,
    limiter: &RateLimiter,
    lookback_hours: i64,
    cap: i64,
) -> Result<()> {
    for rule in load_rules(pool).await? {
        limiter.acquire().await;
        let outcome = adapter
            .fetch(
                &rule,
                Utc::now() - TimeDelta::hours(lookback_hours),
                usize::try_from(cap.max(1)).unwrap_or(20),
            )
            .await;
        match outcome {
            Ok(outcome) => {
                let status = status_name(outcome.status);
                increment_provider_request(adapter.provider_name(), status);
                increment_global_provider_request(adapter.provider_name(), "events", status);
                if outcome.status == ProviderStatus::RateLimited {
                    increment_rate_limit("global-events");
                    warn!(rule_id = rule.id, ?outcome.retry_after, "GDELT rate limited");
                    continue;
                }
                if !outcome.items.is_empty() {
                    let latest = outcome.items.iter().map(|event| event.occurred_at).max();
                    let linked = store_rule_events(pool, &rule, &outcome.items, cap).await?;
                    increment_global_rule_matches(adapter.provider_name(), &rule.symbol, linked);
                    if linked > 0 {
                        if let Some(latest) = latest {
                            set_global_last_success(
                                adapter.provider_name(),
                                "events",
                                latest.timestamp() as f64,
                            );
                        }
                    }
                    info!(rule_id = rule.id, linked, "stored global events");
                }
            }
            Err(error) => {
                increment_provider_request(adapter.provider_name(), "transient_error");
                increment_global_provider_request(adapter.provider_name(), "events", "error");
                error!(rule_id = rule.id, %error, "event provider request failed");
            }
        }
    }
    Ok(())
}

fn status_name(status: ProviderStatus) -> &'static str {
    match status {
        ProviderStatus::Success => "success",
        ProviderStatus::NoData => "no_data",
        ProviderStatus::RateLimited => "rate_limited",
        ProviderStatus::Blocked => "blocked",
        ProviderStatus::TransientError => "transient_error",
        ProviderStatus::PermanentError => "permanent_error",
    }
}

fn env_u64(key: &str, default: u64) -> u64 {
    env::var(key)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}
