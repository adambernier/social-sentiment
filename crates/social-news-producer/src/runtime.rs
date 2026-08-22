use std::{env, sync::Arc, time::Duration};

use anyhow::{bail, Context, Result};
use chrono::{Days, Utc};
use reqwest::{Client, Response, StatusCode};
use social_sentiment_core::{
    config::Config,
    observability::{
        increment_ingested, increment_provider_request, increment_rate_limit,
        initialize_rate_limit, start_metrics_server,
    },
    polling::{PerSymbolBackoff, RateLimiter},
    producer::{
        AdapterOutcome, BoundedCursor, ConfirmedPublisher, ProviderStatus, RawPostPublisher,
        TrackedSymbolRegistry,
    },
    repository::Repository,
    runtime::shutdown_signal,
    symbols::SymbolConfig,
};
use sqlx::postgres::PgPoolOptions;
use tokio::time::Instant;
use tracing::{error, info, warn};

use crate::{adapters, publish_then_commit, PendingUnit};

const REDDIT_FEED: &str = "https://www.reddit.com/r/wallstreetbets+stocks+investing+SecurityAnalysis+options+StockMarket+semiconductors+Spacestocks/comments.json?limit=100";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Provider {
    Bluesky,
    Stocktwits,
    Reddit,
    Finnhub,
    Alpaca,
}

impl Provider {
    fn name(self) -> &'static str {
        match self {
            Self::Bluesky => "bluesky",
            Self::Stocktwits => "stocktwits",
            Self::Reddit => "reddit",
            Self::Finnhub => "finnhub",
            Self::Alpaca => "alpaca",
        }
    }

    fn env_prefix(self) -> &'static str {
        match self {
            Self::Bluesky => "BLUESKY",
            Self::Stocktwits => "STOCKTWITS",
            Self::Reddit => "REDDIT",
            Self::Finnhub => "FINNHUB",
            Self::Alpaca => "ALPACA",
        }
    }

    fn metrics_port(self) -> u16 {
        match self {
            Self::Bluesky => 8001,
            Self::Finnhub => 8002,
            Self::Alpaca => 8003,
            Self::Reddit => 8005,
            Self::Stocktwits => 8006,
        }
    }

    fn rate_per_minute(self) -> f64 {
        let default = match self {
            Self::Bluesky => 30,
            Self::Stocktwits => 60,
            Self::Reddit => 60,
            Self::Finnhub => 55,
            Self::Alpaca => 150,
        };
        env_u64(&format!("{}_RATE_PER_MIN", self.env_prefix()), default) as f64
    }
}

struct State {
    cursor: BoundedCursor<String, String>,
    limiter: Arc<RateLimiter>,
    backoff: PerSymbolBackoff,
}

pub async fn run(provider: Provider) -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    validate_credentials(provider)?;
    let config = Config::from_env();
    initialize_rate_limit(provider.name());
    if provider == Provider::Finnhub && env::var("FINNHUB_API_KEY").unwrap_or_default().is_empty() {
        let _metrics = start_metrics_server(provider.metrics_port(), provider.name());
        error!("FINNHUB_API_KEY is not set; producer is idling without provider requests");
        shutdown_signal().await;
        return Ok(());
    }
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect_lazy(&config.database_dsn)?;
    let registry = TrackedSymbolRegistry::new(Repository::new(pool));
    if let Err(error) = registry.refresh().await {
        warn!(%error, "initial symbol refresh failed; retry loop will recover");
    }
    let refresh_task = registry.spawn_refresh_loop(Duration::from_secs(60));
    let _metrics = start_metrics_server(provider.metrics_port(), provider.name());
    let poll_interval = Duration::from_secs(env_u64(
        &format!("{}_POLL_INTERVAL", provider.env_prefix()),
        900,
    ));
    let maximum_backoff = Duration::from_secs(env_u64(
        &format!("{}_MAX_BACKOFF", provider.env_prefix()),
        3_600,
    ));
    let mut state = State {
        cursor: BoundedCursor::new(2_000),
        limiter: Arc::new(RateLimiter::new(
            provider.rate_per_minute(),
            Duration::from_secs(60),
        )),
        backoff: PerSymbolBackoff::new(poll_interval, maximum_backoff),
    };
    let client = build_client(provider)?;
    if provider == Provider::Reddit {
        reddit_startup_drain(&client, &mut state.cursor).await;
    }

    info!(provider = provider.name(), "native producer started");
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    loop {
        let publisher = match ConfirmedPublisher::connect(
            &config.rabbit_url(),
            &config.queue_raw_posts,
        )
        .await
        {
            Ok(publisher) => publisher,
            Err(error) => {
                warn!(%error, "RabbitMQ connection failed; retrying");
                tokio::select! {
                    () = tokio::time::sleep(Duration::from_secs(10)) => continue,
                    () = &mut shutdown => break,
                }
            }
        };
        let result = tokio::select! {
            result = run_session(provider, &client, &registry, &publisher, &mut state, poll_interval, maximum_backoff) => Some(result),
            () = &mut shutdown => None,
        };
        match result {
            None => break,
            Some(Ok(())) => bail!("producer session stopped unexpectedly"),
            Some(Err(error)) => {
                error!(%error, "producer session failed; reconnecting");
                tokio::select! {
                    () = tokio::time::sleep(Duration::from_secs(10)) => {},
                    () = &mut shutdown => break,
                }
            }
        }
    }
    refresh_task.abort();
    info!(provider = provider.name(), "producer stopped");
    Ok(())
}

async fn run_session(
    provider: Provider,
    client: &Client,
    registry: &TrackedSymbolRegistry,
    publisher: &dyn RawPostPublisher,
    state: &mut State,
    poll_interval: Duration,
    maximum_backoff: Duration,
) -> Result<()> {
    let mut cycle_backoff = poll_interval;
    loop {
        let symbols = registry.snapshot().await;
        let outcomes = poll(provider, client, &symbols, state).await?;
        let mut worst = ProviderStatus::NoData;
        let mut retry_after = None;
        for outcome in outcomes {
            record_outcome(provider, outcome.status);
            let cycle_status = if matches!(
                provider,
                Provider::Stocktwits | Provider::Finnhub | Provider::Alpaca
            ) && outcome.status == ProviderStatus::RateLimited
            {
                ProviderStatus::Success
            } else {
                outcome.status
            };
            worst = merge_status(worst, cycle_status);
            retry_after = retry_after.or(outcome.retry_after);
            if let Some(detail) = &outcome.detail {
                warn!(provider = provider.name(), %detail, "provider response rejected");
            }
            for unit in outcome.items {
                let labels = unit
                    .posts
                    .iter()
                    .map(|post| (post.platform.clone(), post.symbol.clone()))
                    .collect::<Vec<_>>();
                publish_then_commit(publisher, &mut state.cursor, unit).await?;
                for (platform, symbol) in labels {
                    increment_ingested(&platform, &symbol, 1);
                }
            }
        }
        let delay = match worst {
            ProviderStatus::RateLimited | ProviderStatus::Blocked => {
                cycle_backoff = (cycle_backoff * 2).min(maximum_backoff);
                retry_after.unwrap_or(cycle_backoff).min(maximum_backoff)
            }
            ProviderStatus::TransientError if provider == Provider::Reddit => {
                Duration::from_secs(env_u64("REDDIT_TRANSIENT_RETRY_INTERVAL", 60))
                    .min(poll_interval)
            }
            _ => {
                cycle_backoff = poll_interval;
                poll_interval
            }
        };
        info!(
            provider = provider.name(),
            delay_seconds = delay.as_secs(),
            "poll complete"
        );
        tokio::time::sleep(delay).await;
    }
}

async fn poll(
    provider: Provider,
    client: &Client,
    symbols: &[SymbolConfig],
    state: &mut State,
) -> Result<Vec<AdapterOutcome<PendingUnit>>> {
    let mut outcomes = Vec::new();
    match provider {
        Provider::Bluesky => {
            for symbol in symbols {
                for term in symbol.search_terms() {
                    state.limiter.acquire().await;
                    let mut request = client
                        .get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts")
                        .query(&[("q", term.as_str()), ("limit", "25"), ("sort", "latest")]);
                    if let Some(since) = state.cursor.get(&term) {
                        request = request.query(&[("since", since)]);
                    }
                    let response = request.send().await?;
                    outcomes.push(
                        parse_response(response, |bytes| {
                            adapters::bluesky::parse(
                                bytes,
                                symbol,
                                &term,
                                state.cursor.get(&term).map(String::as_str),
                                Utc::now(),
                            )
                        })
                        .await?,
                    );
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
        Provider::Stocktwits => {
            for symbol in due_symbols(symbols, &state.backoff) {
                state.limiter.acquire().await;
                let url = format!(
                    "https://api.stocktwits.com/api/2/streams/symbol/{}.json",
                    symbol.symbol
                );
                let mut request = client.get(url);
                if let Some(since) = state
                    .cursor
                    .get(&symbol.symbol)
                    .filter(|value| value.as_str() != "0")
                {
                    request = request.query(&[("since", since)]);
                }
                let response = request.send().await?;
                let outcome = parse_response(response, |bytes| {
                    adapters::stocktwits::parse(bytes, &symbol.symbol, Utc::now())
                })
                .await?;
                state.backoff.record_with_retry_after(
                    &symbol.symbol,
                    outcome.status == ProviderStatus::RateLimited,
                    outcome.retry_after,
                    Instant::now(),
                );
                outcomes.push(outcome);
            }
        }
        Provider::Reddit => {
            state.limiter.acquire().await;
            let response = client.get(REDDIT_FEED).send().await?;
            outcomes.push(
                parse_response(response, |bytes| {
                    adapters::reddit::parse(
                        bytes,
                        symbols,
                        |id| state.cursor.contains_key(&id.to_owned()),
                        Utc::now(),
                    )
                })
                .await?,
            );
        }
        Provider::Finnhub => {
            let today = Utc::now().date_naive();
            let from = today
                .checked_sub_days(Days::new(env_u64("FINNHUB_LOOKBACK_DAYS", 3)))
                .unwrap_or(today);
            let token = env::var("FINNHUB_API_KEY").unwrap_or_default();
            for symbol in due_symbols(symbols, &state.backoff) {
                state.limiter.acquire().await;
                let response = client
                    .get("https://finnhub.io/api/v1/company-news")
                    .query(&[
                        ("symbol", symbol.symbol.as_str()),
                        ("from", &from.to_string()),
                        ("to", &today.to_string()),
                        ("token", token.as_str()),
                    ])
                    .send()
                    .await?;
                let outcome = parse_response(response, |bytes| {
                    adapters::finnhub::parse(
                        bytes,
                        &symbol,
                        |id| state.cursor.contains_key(&id.to_owned()),
                        Utc::now(),
                    )
                })
                .await?;
                state.backoff.record_with_retry_after(
                    &symbol.symbol,
                    outcome.status == ProviderStatus::RateLimited,
                    outcome.retry_after,
                    Instant::now(),
                );
                outcomes.push(outcome);
            }
        }
        Provider::Alpaca => {
            let key = env::var("ALPACA_API_KEY").unwrap_or_default();
            let secret = env::var("ALPACA_API_SECRET").unwrap_or_default();
            let url = env::var("ALPACA_URL")
                .unwrap_or_else(|_| "https://data.alpaca.markets/v1beta1/news".into());
            for symbol in due_symbols(symbols, &state.backoff) {
                state.limiter.acquire().await;
                let response = client
                    .get(&url)
                    .header("APCA-API-KEY-ID", &key)
                    .header("APCA-API-SECRET-KEY", &secret)
                    .query(&[("symbols", symbol.symbol.as_str()), ("limit", "50")])
                    .send()
                    .await?;
                let outcome = parse_response(response, |bytes| {
                    adapters::alpaca::parse(
                        bytes,
                        &symbol,
                        |id| state.cursor.contains_key(&id.to_owned()),
                        Utc::now(),
                    )
                })
                .await?;
                state.backoff.record_with_retry_after(
                    &symbol.symbol,
                    outcome.status == ProviderStatus::RateLimited,
                    outcome.retry_after,
                    Instant::now(),
                );
                outcomes.push(outcome);
            }
        }
    }
    Ok(outcomes)
}

fn due_symbols(symbols: &[SymbolConfig], backoff: &PerSymbolBackoff) -> Vec<SymbolConfig> {
    let now = Instant::now();
    symbols
        .iter()
        .filter(|symbol| backoff.is_due(&symbol.symbol, now))
        .cloned()
        .collect()
}

async fn parse_response<F>(response: Response, parser: F) -> Result<AdapterOutcome<PendingUnit>>
where
    F: FnOnce(&[u8]) -> AdapterOutcome<PendingUnit>,
{
    let status = response.status();
    let retry_after = response
        .headers()
        .get("retry-after")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|seconds| *seconds > 0)
        .map(Duration::from_secs);
    if !status.is_success() {
        let provider_status = match status {
            StatusCode::FORBIDDEN => ProviderStatus::Blocked,
            StatusCode::TOO_MANY_REQUESTS => ProviderStatus::RateLimited,
            status if status.is_server_error() => ProviderStatus::TransientError,
            _ => ProviderStatus::PermanentError,
        };
        return Ok(AdapterOutcome::failure(provider_status, retry_after));
    }
    let body = response.bytes().await.context("read provider response")?;
    Ok(parser(&body))
}

async fn reddit_startup_drain(client: &Client, cursor: &mut BoundedCursor<String, String>) {
    let result = async {
        let response = client.get(REDDIT_FEED).send().await?.error_for_status()?;
        let value: serde_json::Value = response.json().await?;
        if let Some(children) = value
            .pointer("/data/children")
            .and_then(serde_json::Value::as_array)
        {
            for child in children {
                if let Some(id) = child
                    .pointer("/data/id")
                    .and_then(serde_json::Value::as_str)
                {
                    cursor.commit(id.to_owned(), id.to_owned());
                }
            }
        }
        Ok::<(), reqwest::Error>(())
    }
    .await;
    if let Err(error) = result {
        warn!(%error, "Reddit startup drain failed; continuing");
    }
}

fn validate_credentials(provider: Provider) -> Result<()> {
    if provider == Provider::Alpaca
        && (env::var("ALPACA_API_KEY").unwrap_or_default().is_empty()
            || env::var("ALPACA_API_SECRET").unwrap_or_default().is_empty())
    {
        bail!("ALPACA_API_KEY and ALPACA_API_SECRET must be set");
    }
    Ok(())
}

fn build_client(provider: Provider) -> Result<Client> {
    let mut builder =
        Client::builder().timeout(Duration::from_secs(if provider == Provider::Reddit {
            15
        } else {
            10
        }));
    if provider == Provider::Reddit {
        builder = builder.user_agent(
            env::var("REDDIT_USER_AGENT")
                .unwrap_or_else(|_| "social-sentiment-rss/0.1 (sentiment dashboard)".into()),
        );
        if let Ok(proxy) = env::var("REDDIT_PROXY_URL") {
            if !proxy.is_empty() {
                builder = builder.proxy(reqwest::Proxy::all(proxy)?);
            }
        }
    }
    Ok(builder.build()?)
}

fn record_outcome(provider: Provider, status: ProviderStatus) {
    let status_name = match status {
        ProviderStatus::Success => "success",
        ProviderStatus::NoData => "no_data",
        ProviderStatus::RateLimited => "rate_limited",
        ProviderStatus::Blocked => "blocked",
        ProviderStatus::TransientError => "transient_error",
        ProviderStatus::PermanentError => "permanent_error",
    };
    increment_provider_request(provider.name(), status_name);
    if status == ProviderStatus::RateLimited {
        increment_rate_limit(provider.name());
    }
}

fn merge_status(left: ProviderStatus, right: ProviderStatus) -> ProviderStatus {
    fn rank(status: ProviderStatus) -> u8 {
        match status {
            ProviderStatus::NoData => 0,
            ProviderStatus::Success => 1,
            ProviderStatus::PermanentError => 2,
            ProviderStatus::TransientError => 3,
            ProviderStatus::Blocked => 4,
            ProviderStatus::RateLimited => 5,
        }
    }
    if rank(right) > rank(left) {
        right
    } else {
        left
    }
}

fn env_u64(key: &str, default: u64) -> u64 {
    env::var(key)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}
