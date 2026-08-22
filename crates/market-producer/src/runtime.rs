use std::{collections::HashMap, env, sync::Arc, time::Duration};

use anyhow::{Context, Result};
use chrono::{DateTime, Datelike, NaiveDate, NaiveTime, TimeDelta, Timelike, Utc};
use chrono_tz::America::New_York;
use chrono_tz::Tz;
use serde_json::Value;
use social_sentiment_core::{
    config::Config,
    observability::{
        increment_global_provider_request, increment_ingested, increment_provider_request,
        increment_rate_limit, initialize_rate_limit, set_global_last_success, start_metrics_server,
    },
    polling::{PerSymbolBackoff, RateLimiter},
    producer::{ProviderStatus, TrackedSymbolRegistry},
    repository::Repository,
    runtime::shutdown_signal,
    schemas::StockQuote,
};
use sqlx::{postgres::PgPoolOptions, types::Json, PgPool, Row};
use tokio::time::Instant;
use tracing::{error, warn};

use crate::{
    adapters::{taiwan::TaiwanIndexProvider, yahoo::YahooProvider, RoutedGlobalBarProvider},
    sessions::UsMarketCalendar,
    synthetic_nav::{
        estimate_pct_change, official_close_timestamp, official_is_final, valid_baseline, HOLDINGS,
        MAX_PLAUSIBLE_MOVE, NAV_SYMBOL,
    },
    FundamentalsProvider, GlobalBarProvider, MarketInstrument, QuoteProvider, SessionCalendar,
};

pub async fn run() -> Result<()> {
    let config = Config::from_env();
    initialize_rate_limit("market");
    initialize_rate_limit("global-market");
    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&config.database_dsn)
        .await?;
    let repository = Repository::new(pool.clone());
    let registry = TrackedSymbolRegistry::new(repository.clone());
    if let Err(error) = registry.refresh().await {
        warn!(%error, "initial symbol refresh failed; retry loop will recover");
    }
    let refresh = registry.spawn_refresh_loop(Duration::from_secs(60));
    let _metrics = start_metrics_server(8003, "market");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent("social-sentiment-market/1.0")
        .build()?;
    let yahoo = YahooProvider::new(client.clone());
    let global = RoutedGlobalBarProvider {
        taiwan: TaiwanIndexProvider::new(client),
        yahoo: yahoo.clone(),
    };
    let limiter = Arc::new(RateLimiter::new(
        env_u64("MARKET_RATE_PER_MIN", 120) as f64,
        Duration::from_secs(60),
    ));
    let global_interval_seconds = env_u64("GLOBAL_MARKET_POLL_SECONDS", 900);
    let global_limiter = RateLimiter::new(
        env_u64("GLOBAL_MARKET_RATE_PER_MINUTE", 20) as f64,
        Duration::from_secs(60),
    );
    let mut backoff = PerSymbolBackoff::new(
        Duration::from_secs(60),
        Duration::from_secs(env_u64("MARKET_MAX_BACKOFF", 3_600)),
    );
    let mut global_backoff = PerSymbolBackoff::new(
        Duration::from_secs(global_interval_seconds),
        Duration::from_secs(env_u64("GLOBAL_MARKET_MAX_BACKOFF", 14_400)),
    );
    let mut last_daily_session = HashMap::new();
    let mut last_metrics = DateTime::<Utc>::MIN_UTC;
    let mut last_global = DateTime::<Utc>::MIN_UTC;
    let mut last_nav = DateTime::<Utc>::MIN_UTC;
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    loop {
        let now = Utc::now();
        let symbols = registry.snapshot().await;
        if let Err(error) = poll_quotes(
            &pool,
            &yahoo,
            &UsMarketCalendar,
            &symbols,
            &limiter,
            &mut backoff,
            now,
            &config.vix_symbol,
        )
        .await
        {
            error!(%error, "quote polling failed; next cycle will retry");
        }
        if now - last_metrics >= TimeDelta::hours(1) {
            if let Err(error) = poll_fundamentals(&repository, &yahoo, &symbols, &limiter).await {
                error!(%error, "fundamentals polling failed");
            }
            last_metrics = now;
        }
        if now - last_nav
            >= TimeDelta::seconds(
                i64::try_from(env_u64("SYNTHETIC_NAV_INTERVAL", 300)).unwrap_or(300),
            )
        {
            if let Err(error) =
                poll_synthetic_nav(&pool, &yahoo, &UsMarketCalendar, &limiter, now).await
            {
                error!(%error, "synthetic NAV polling failed");
            }
            last_nav = now;
        }
        if config.global_context_enabled
            && now - last_global
                >= TimeDelta::seconds(i64::try_from(global_interval_seconds).unwrap_or(900))
        {
            if let Err(error) = poll_global_bars(
                &repository,
                &global,
                now,
                &global_limiter,
                &mut global_backoff,
                &mut last_daily_session,
                env_u64("GLOBAL_DAILY_SETTLE_GRACE_MINUTES", 30),
            )
            .await
            {
                error!(%error, "global market polling failed; core quotes continue");
            }
            last_global = now;
        }
        tokio::select! {
            () = tokio::time::sleep(Duration::from_secs(60)) => {},
            () = &mut shutdown => break,
        }
    }
    refresh.abort();
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn poll_quotes(
    pool: &PgPool,
    provider: &dyn QuoteProvider,
    calendar: &dyn SessionCalendar,
    symbols: &[social_sentiment_core::symbols::SymbolConfig],
    limiter: &RateLimiter,
    backoff: &mut PerSymbolBackoff,
    now: DateTime<Utc>,
    vix_symbol: &str,
) -> Result<()> {
    let mut poll_symbols = symbols
        .iter()
        .filter(|symbol| symbol.symbol != NAV_SYMBOL)
        .map(|symbol| symbol.symbol.clone())
        .collect::<Vec<_>>();
    poll_symbols.extend(symbols.iter().filter_map(|symbol| symbol.future.clone()));
    poll_symbols.push(vix_symbol.to_owned());
    poll_symbols.sort();
    poll_symbols.dedup();
    for symbol in poll_symbols {
        if !backoff.is_due(&symbol, Instant::now()) {
            continue;
        }
        let session = if symbol.ends_with("=F") {
            calendar.futures_session(&symbol, now)
        } else {
            calendar.equity_session(now)
        };
        if matches!(
            session.as_str(),
            "closed" | "futures_closed" | "futures_break"
        ) {
            increment_provider_request(provider.provider_name(), "no_data");
            continue;
        }
        limiter.acquire().await;
        let outcome = provider.fetch_quote(&symbol, &session).await?;
        record_provider_status("market", provider.provider_name(), outcome.status);
        backoff.record(
            &symbol,
            outcome.status == ProviderStatus::RateLimited,
            Instant::now(),
        );
        for quote in outcome.items {
            if insert_quote(pool, &quote).await? {
                increment_ingested("market", &quote.symbol, 1);
            }
        }
    }
    Ok(())
}

async fn insert_quote(pool: &PgPool, quote: &StockQuote) -> Result<bool> {
    let inserted = sqlx::query_scalar::<_, String>(
        r#"
        INSERT INTO stock_quotes (symbol, timestamp, price, volume, market_session)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (symbol, timestamp) DO NOTHING
        RETURNING symbol
        "#,
    )
    .bind(&quote.symbol)
    .bind(quote.timestamp)
    .bind(quote.price)
    .bind(quote.volume)
    .bind(&quote.market_session)
    .fetch_optional(pool)
    .await?;
    Ok(inserted.is_some())
}

async fn poll_fundamentals(
    repository: &Repository,
    provider: &dyn FundamentalsProvider,
    symbols: &[social_sentiment_core::symbols::SymbolConfig],
    limiter: &RateLimiter,
) -> Result<()> {
    for symbol in symbols {
        if symbol.symbol.ends_with("=F") || symbol.symbol.starts_with('^') {
            continue;
        }
        limiter.acquire().await;
        let baseline = symbol.sector.as_deref().unwrap_or("SPY");
        let outcome = provider
            .fetch_fundamentals(&symbol.symbol, baseline)
            .await?;
        record_provider_status("market", "yahoo-fundamentals", outcome.status);
        for metrics in outcome.items {
            repository.upsert_metrics(&metrics).await?;
        }
    }
    Ok(())
}

async fn poll_global_bars(
    repository: &Repository,
    provider: &dyn GlobalBarProvider,
    now: DateTime<Utc>,
    limiter: &RateLimiter,
    backoff: &mut PerSymbolBackoff,
    last_daily_session: &mut HashMap<String, NaiveDate>,
    daily_grace_minutes: u64,
) -> Result<()> {
    ensure_us_equity_instruments(repository.pool()).await?;
    let instruments = load_global_instruments(repository.pool()).await?;
    for instrument in instruments {
        for interval in ["1h", "1d"] {
            if instrument.asset_class == "us_equity" && interval == "1h" {
                continue;
            }
            if interval == "1d"
                && !daily_is_due(
                    &instrument,
                    now,
                    last_daily_session.get(&instrument.instrument_key),
                    daily_grace_minutes,
                )
            {
                continue;
            }
            let Some(provider_name) = provider.provider_name_for(&instrument, interval) else {
                continue;
            };
            let request_key = format!("{}:{interval}", instrument.instrument_key);
            if !backoff.is_due(&request_key, Instant::now()) {
                continue;
            }
            limiter.acquire().await;
            let lookback = if interval == "1h" {
                TimeDelta::days(3)
            } else {
                TimeDelta::days(10)
            };
            let outcome = provider
                .fetch_bars(
                    &instrument,
                    interval,
                    now - lookback,
                    now + TimeDelta::days(1),
                )
                .await?;
            record_provider_status("global-market", provider_name, outcome.status);
            let data_type = format!("bars_{interval}");
            increment_global_provider_request(
                provider_name,
                &data_type,
                status_name(outcome.status),
            );
            backoff.record(
                &request_key,
                outcome.status == ProviderStatus::RateLimited,
                Instant::now(),
            );
            let completed = matches!(
                outcome.status,
                ProviderStatus::Success | ProviderStatus::NoData
            );
            let latest = outcome.items.iter().map(|bar| bar.ends_at).max();
            for bar in outcome.items {
                repository.upsert_global_bar(&bar).await?;
            }
            if let Some(latest) = latest {
                set_global_last_success(provider_name, &data_type, latest.timestamp() as f64);
            }
            if interval == "1d" && completed {
                let timezone: Tz = instrument.timezone.parse()?;
                last_daily_session.insert(
                    instrument.instrument_key.clone(),
                    now.with_timezone(&timezone).date_naive(),
                );
            }
        }
    }
    Ok(())
}

fn daily_is_due(
    instrument: &MarketInstrument,
    now: DateTime<Utc>,
    last_session: Option<&NaiveDate>,
    grace_minutes: u64,
) -> bool {
    let Ok(timezone) = instrument.timezone.parse::<Tz>() else {
        return false;
    };
    let local = now.with_timezone(&timezone);
    let weekdays = instrument
        .session_metadata
        .get("weekdays")
        .and_then(Value::as_array)
        .map(|values| values.iter().filter_map(Value::as_u64).collect::<Vec<_>>())
        .unwrap_or_else(|| vec![1, 2, 3, 4, 5]);
    if !weekdays.contains(&u64::from(local.weekday().number_from_monday()))
        || last_session == Some(&local.date_naive())
    {
        return false;
    }
    let close = instrument
        .session_metadata
        .get("close")
        .and_then(Value::as_str)
        .and_then(|value| NaiveTime::parse_from_str(value, "%H:%M").ok())
        .unwrap_or_else(|| NaiveTime::from_hms_opt(23, 59, 0).unwrap());
    let grace = TimeDelta::minutes(i64::try_from(grace_minutes).unwrap_or(30));
    local.naive_local() >= local.date_naive().and_time(close) + grace
}

pub async fn backfill_global(
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    interval: &str,
) -> Result<()> {
    let config = Config::from_env();
    if !config.global_context_enabled {
        anyhow::bail!("GLOBAL_CONTEXT_ENABLED must be true for backfill");
    }
    let pool = PgPoolOptions::new().connect(&config.database_dsn).await?;
    let repository = Repository::new(pool.clone());
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let provider = RoutedGlobalBarProvider {
        taiwan: TaiwanIndexProvider::new(client.clone()),
        yahoo: YahooProvider::new(client),
    };
    let limiter = RateLimiter::new(20.0, Duration::from_secs(60));
    for instrument in load_global_instruments(&pool).await? {
        if provider.provider_name_for(&instrument, interval).is_none() {
            continue;
        }
        limiter.acquire().await;
        let outcome = provider
            .fetch_bars(&instrument, interval, start, end)
            .await?;
        for bar in outcome.items {
            repository.upsert_global_bar(&bar).await?;
        }
    }
    Ok(())
}

pub async fn sync_catalog() -> Result<()> {
    let config = Config::from_env();
    let pool = PgPoolOptions::new().connect(&config.database_dsn).await?;
    ensure_us_equity_instruments(&pool).await
}

async fn ensure_us_equity_instruments(pool: &PgPool) -> Result<()> {
    sqlx::query(
        r#"
        INSERT INTO global_instruments (
            instrument_key, display_name, asset_class, currency, exchange,
            timezone, provider_aliases, session_metadata
        )
        SELECT 'equity:' || lower(symbol), symbol, 'us_equity', 'USD', 'US',
               'America/New_York', jsonb_build_object('yahoo', symbol),
               '{"open":"09:30","close":"16:00","weekdays":[1,2,3,4,5]}'::jsonb
        FROM tracked_symbols WHERE is_active
        ON CONFLICT (instrument_key) DO UPDATE SET
            display_name=EXCLUDED.display_name,
            provider_aliases=EXCLUDED.provider_aliases,
            is_active=TRUE,
            updated_at=NOW()
        "#,
    )
    .execute(pool)
    .await?;
    Ok(())
}

async fn load_global_instruments(pool: &PgPool) -> Result<Vec<MarketInstrument>> {
    let rows = sqlx::query(
        r#"
        SELECT instrument_key, display_name, asset_class, currency, exchange,
               timezone, provider_aliases, session_metadata, quote_convention
        FROM global_instruments WHERE is_active ORDER BY instrument_key
        "#,
    )
    .fetch_all(pool)
    .await?;
    rows.into_iter()
        .map(|row| {
            let aliases = row.get::<Json<Value>, _>("provider_aliases").0;
            let sessions = row.get::<Json<Value>, _>("session_metadata").0;
            Ok(MarketInstrument {
                instrument_key: row.get("instrument_key"),
                display_name: row.get("display_name"),
                asset_class: row.get("asset_class"),
                currency: row.get("currency"),
                exchange: row.get("exchange"),
                timezone: row.get("timezone"),
                provider_aliases: aliases
                    .as_object()
                    .cloned()
                    .context("provider_aliases must be an object")?,
                session_metadata: sessions
                    .as_object()
                    .cloned()
                    .context("session_metadata must be an object")?,
                quote_convention: row.get("quote_convention"),
            })
        })
        .collect()
}

async fn poll_synthetic_nav(
    pool: &PgPool,
    yahoo: &YahooProvider,
    calendar: &dyn SessionCalendar,
    limiter: &RateLimiter,
    now: DateTime<Utc>,
) -> Result<()> {
    let session = calendar.equity_session(now);
    let today = now.with_timezone(&New_York).date_naive();
    let official_due = calendar.is_trading_day(today) && official_is_final(today, now);
    if session != "regular" && !official_due {
        return Ok(());
    }
    limiter.acquire().await;
    let nav_outcome = yahoo.daily_closes(NAV_SYMBOL, "5d").await?;
    record_provider_status("market", "yahoo-nav", nav_outcome.status);
    let navs = nav_outcome.items;
    for (index, nav) in navs.iter().enumerate() {
        if !calendar.is_trading_day(nav.date) || !official_is_final(nav.date, now) {
            continue;
        }
        if let Some(previous) = index.checked_sub(1).and_then(|previous| navs.get(previous)) {
            let movement = (nav.close - previous.close).abs() / previous.close * 100.0;
            if movement > MAX_PLAUSIBLE_MOVE || (nav.date == today && nav.close == previous.close) {
                continue;
            }
        }
        let quote = StockQuote {
            symbol: NAV_SYMBOL.into(),
            timestamp: official_close_timestamp(nav.date),
            price: round_four(nav.close),
            volume: 0,
            market_session: "regular".into(),
            provider: "yfinance".into(),
        };
        if insert_quote(pool, &quote).await? {
            increment_ingested("market", NAV_SYMBOL, 1);
        }
    }
    if session != "regular" {
        return Ok(());
    }
    let Some(baseline) = valid_baseline(&navs, today, calendar) else {
        return Ok(());
    };
    let mut histories = HashMap::new();
    for (symbol, _) in HOLDINGS {
        limiter.acquire().await;
        let outcome = yahoo.daily_closes(symbol, "5d").await?;
        record_provider_status("market", "yahoo-nav-proxy", outcome.status);
        histories.insert(symbol.to_owned(), outcome.items);
    }
    let Some(change) = estimate_pct_change(&histories, today) else {
        return Ok(());
    };
    let timestamp = now
        .with_second(0)
        .and_then(|value| value.with_nanosecond(0))
        .unwrap_or(now);
    if timestamp >= official_close_timestamp(today) {
        return Ok(());
    }
    let quote = StockQuote {
        symbol: NAV_SYMBOL.into(),
        timestamp,
        price: round_four(baseline.close * (1.0 + change / 100.0)),
        volume: 0,
        market_session: "regular".into(),
        provider: "synthetic-nav".into(),
    };
    if insert_quote(pool, &quote).await? {
        increment_ingested("market", NAV_SYMBOL, 1);
    }
    Ok(())
}

fn round_four(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

fn record_provider_status(platform: &str, provider: &str, status: ProviderStatus) {
    let status_name = status_name(status);
    increment_provider_request(provider, status_name);
    if status == ProviderStatus::RateLimited {
        increment_rate_limit(platform);
    }
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
