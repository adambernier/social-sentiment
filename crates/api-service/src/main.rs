mod admin;
mod api_error;
mod docs;
mod global_context;

use anyhow::Result;
use axum::{
    extract::{ws::Message, ws::WebSocket, Query, State, WebSocketUpgrade},
    http::{HeaderValue, StatusCode},
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use chrono::{DateTime, Timelike, Utc};
use serde::{Deserialize, Serialize};
use social_sentiment_core::{
    config::Config, observability::metrics_body, runtime::shutdown_signal,
};
use sqlx::{
    postgres::{PgListener, PgPoolOptions},
    PgPool, Row,
};
use std::net::SocketAddr;
use tokio::sync::broadcast;
use tower_http::cors::{AllowOrigin, Any, CorsLayer};
use tracing::{error, info, warn};

#[derive(Clone)]
pub(crate) struct AppState {
    pub(crate) pool: PgPool,
    post_events: broadcast::Sender<String>,
    vix_symbol: String,
    pub(crate) admin_api_key: String,
    pub(crate) global_context_enabled: bool,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct PostResponse {
    id: String,
    symbol: String,
    platform: String,
    text: String,
    timestamp: DateTime<Utc>,
    sentiment: String,
    scores: serde_json::Value,
    topic_id: Option<i32>,
    topic_label: Option<String>,
    scored_at: DateTime<Utc>,
    engagement: i32,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct TopicStats {
    topic_label: Option<String>,
    count: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct SentimentStats {
    sentiment: String,
    count: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct LeaderboardEntry {
    symbol: String,
    post_count_4h: i64,
    sentiment_index_4h: f64,
    buzz_z: Option<f64>,
    baseline_hourly: f64,
    baseline_samples: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct SourceHealth {
    platform: String,
    posts_1h: i64,
    posts_24h: i64,
    last_ingest: Option<DateTime<Utc>>,
    age_seconds: Option<f64>,
    baseline_per_hour: Option<f64>,
    status: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct MarketQuote {
    symbol: String,
    timestamp: DateTime<Utc>,
    price: f64,
    volume: i64,
    market_session: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct MarketDelta {
    symbol: String,
    reference_price: f64,
    latest_price: f64,
    pct_change: f64,
    abs_change: f64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct StockMetricsResponse {
    symbol: String,
    pe_ratio: Option<f64>,
    beta: Option<f64>,
    avg_return_1y: Option<f64>,
    inflation_adj_return_1y: Option<f64>,
    pe_relative_sector: Option<f64>,
    beta_relative_sector: Option<f64>,
    return_relative_sector: Option<f64>,
    updated_at: DateTime<Utc>,
}

#[derive(Debug, Serialize, Deserialize)]
struct DashboardResponse {
    sentiment_stats: Vec<SentimentStats>,
    topic_stats: Vec<TopicStats>,
    posts: Vec<PostResponse>,
    market_data: Vec<MarketQuote>,
    latest_quote: Option<MarketQuote>,
    metrics_data: Option<StockMetricsResponse>,
    primary_delta: Option<MarketDelta>,
    primary_future_symbol: Option<String>,
    primary_future_quote: Option<MarketQuote>,
    primary_future_delta: Option<MarketDelta>,
    primary_future_market_data: Vec<MarketQuote>,
    vix_quote: Option<MarketQuote>,
    vix_delta: Option<MarketDelta>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct CorrelationBucket {
    timestamp: String,
    positive: i64,
    neutral: i64,
    negative: i64,
    #[serde(rename = "priceChange")]
    price_change: Option<f64>,
    #[serde(rename = "pricePct")]
    price_pct: Option<f64>,
    #[serde(rename = "futureChange")]
    future_change: Option<f64>,
    #[serde(rename = "futurePct")]
    future_pct: Option<f64>,
    #[serde(rename = "isMarketOpen")]
    is_market_open: bool,
    #[serde(rename = "sentimentIndex")]
    sentiment_index: f64,
    #[serde(rename = "sentimentSMA")]
    sentiment_sma: f64,
    #[serde(rename = "rawPrice")]
    raw_price: Option<f64>,
    #[serde(rename = "buySignal")]
    buy_signal: Option<bool>,
    #[serde(rename = "signalQuality")]
    signal_quality: Option<String>,
    #[serde(rename = "buyScore")]
    buy_score: Option<f64>,
    #[serde(rename = "supportPrice")]
    support_price: Option<f64>,
    #[serde(rename = "supportPct")]
    support_pct: Option<f64>,
    #[serde(rename = "resistancePrice")]
    resistance_price: Option<f64>,
    #[serde(rename = "resistancePct")]
    resistance_pct: Option<f64>,
    #[serde(rename = "sentimentMACD")]
    sentiment_macd: Option<f64>,
    #[serde(rename = "sentimentSignal")]
    sentiment_signal: Option<f64>,
    #[serde(rename = "sentimentHist")]
    sentiment_hist: Option<f64>,
}

#[derive(Debug, Serialize, Deserialize)]
struct ClosedRegion {
    start: String,
    end: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct LagSweepValue {
    lag: i32,
    r: f64,
}

#[derive(Debug, Serialize, Deserialize)]
struct OpportunityResponse {
    score: f64,
    classification: String,
    color: String,
    strategy: String,
    description: String,
    checklist: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct CorrelationResponse {
    data: Vec<CorrelationBucket>,
    #[serde(rename = "closedRegions")]
    closed_regions: Vec<ClosedRegion>,
    #[serde(rename = "supportPrice")]
    support_price: f64,
    #[serde(rename = "supportPct")]
    support_pct: f64,
    #[serde(rename = "resistancePrice")]
    resistance_price: f64,
    #[serde(rename = "resistancePct")]
    resistance_pct: f64,
    #[serde(rename = "maxR")]
    max_r: f64,
    #[serde(rename = "bestLag")]
    best_lag: i32,
    #[serde(rename = "lagSweeps")]
    lag_sweeps: Vec<LagSweepValue>,
    #[serde(rename = "correlationText")]
    correlation_text: String,
    #[serde(rename = "correlationStrength")]
    correlation_strength: String,
    opportunity: Option<OpportunityResponse>,
    #[serde(rename = "coverageStart")]
    coverage_start: Option<String>,
    #[serde(rename = "coverageEnd")]
    coverage_end: String,
    #[serde(rename = "coverageComplete")]
    coverage_complete: bool,
    #[serde(rename = "coverageMode")]
    coverage_mode: String,
    #[serde(rename = "coverageNotice")]
    coverage_notice: Option<String>,
}

#[derive(Debug, Deserialize)]
struct DashboardQueryParams {
    symbol: Option<String>,
    hours: Option<i64>,
    platform: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CorrelationQueryParams {
    symbol: Option<String>,
    hours: Option<i64>,
    platform: Option<String>,
    topic: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PostsQueryParams {
    symbol: Option<String>,
    platform: Option<String>,
    sentiment: Option<String>,
    topic: Option<String>,
    hour: Option<String>,
    start_time: Option<DateTime<Utc>>,
    end_time: Option<DateTime<Utc>>,
    limit: Option<i64>,
    offset: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct MarketQueryParams {
    symbol: String,
    hours: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct SymbolQueryParams {
    symbol: String,
}

#[derive(Debug, Deserialize)]
struct MarketDeltaQueryParams {
    symbol: String,
    since: DateTime<Utc>,
}

async fn health_handler(State(state): State<AppState>) -> Json<serde_json::Value> {
    match sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(&state.pool)
        .await
    {
        Ok(_) => Json(serde_json::json!({
            "status": "healthy",
            "database": "connected"
        })),
        Err(error) => {
            error!(%error, "health check database query failed");
            Json(serde_json::json!({
                "status": "unhealthy",
                "error": error.to_string()
            }))
        }
    }
}

fn cors_layer_from_env() -> Option<CorsLayer> {
    let origins = std::env::var("CORS_ALLOW_ORIGINS")
        .unwrap_or_default()
        .split(',')
        .filter_map(|origin| {
            let origin = origin.trim();
            (!origin.is_empty())
                .then(|| origin.parse::<HeaderValue>().ok())
                .flatten()
        })
        .collect::<Vec<_>>();
    (!origins.is_empty()).then(|| {
        CorsLayer::new()
            .allow_origin(AllowOrigin::list(origins))
            .allow_methods(Any)
            .allow_headers(Any)
    })
}

async fn prometheus_handler() -> String {
    metrics_body("api")
}

fn internal_error(error: sqlx::Error) -> (StatusCode, String) {
    error!(%error, "database query failed");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        "database query failed".to_string(),
    )
}

fn validated_hours(hours: Option<i64>) -> Result<i64, (StatusCode, String)> {
    let hours = hours.unwrap_or(24);
    if !(1..=8760).contains(&hours) {
        return Err((
            StatusCode::BAD_REQUEST,
            "hours must be between 1 and 8760".to_string(),
        ));
    }
    Ok(hours)
}

#[allow(dead_code)]
fn market_delta(quotes: &[MarketQuote]) -> Option<MarketDelta> {
    let first = quotes.first()?;
    let last = quotes.last()?;
    let abs_change = last.price - first.price;
    let pct_change = if first.price == 0.0 {
        0.0
    } else {
        abs_change / first.price * 100.0
    };
    Some(MarketDelta {
        symbol: last.symbol.clone(),
        reference_price: first.price,
        latest_price: last.price,
        pct_change,
        abs_change,
    })
}

async fn fetch_market_series(
    pool: &PgPool,
    symbol: &str,
    cutoff: DateTime<Utc>,
) -> Result<Vec<MarketQuote>, (StatusCode, String)> {
    let rows = sqlx::query(
        r#"
        SELECT symbol, timestamp, price, volume, market_session
        FROM stock_quotes
        WHERE symbol = $1 AND timestamp >= $2
        ORDER BY timestamp ASC
        "#,
    )
    .bind(symbol)
    .bind(cutoff)
    .fetch_all(pool)
    .await
    .map_err(internal_error)?;

    Ok(rows
        .into_iter()
        .map(|row| MarketQuote {
            symbol: row.get("symbol"),
            timestamp: row.get("timestamp"),
            price: row.get("price"),
            volume: row.get("volume"),
            market_session: row.get("market_session"),
        })
        .collect())
}

async fn fetch_market_snapshot(
    pool: &PgPool,
    symbol: &str,
) -> Result<Option<(MarketQuote, MarketDelta)>, (StatusCode, String)> {
    let row = sqlx::query(
        r#"
        SELECT
            latest.symbol,
            latest.timestamp,
            latest.price,
            latest.volume,
            latest.market_session,
            COALESCE(
                previous_session.price,
                latest_session.price,
                oldest.price,
                latest.price
            ) AS reference_price
        FROM (
            SELECT symbol, timestamp, price, volume, market_session
            FROM stock_quotes
            WHERE symbol = $1
            ORDER BY timestamp DESC
            LIMIT 1
        ) AS latest
        LEFT JOIN LATERAL (
            SELECT timestamp, price
            FROM stock_quotes
            WHERE symbol = $1
              AND market_session = CASE WHEN RIGHT($1, 2) = '=F' THEN 'futures_open' ELSE 'regular' END
            ORDER BY timestamp DESC
            LIMIT 1
        ) AS latest_session ON TRUE
        LEFT JOIN LATERAL (
            SELECT price
            FROM stock_quotes
            WHERE symbol = $1
              AND market_session = CASE WHEN RIGHT($1, 2) = '=F' THEN 'futures_open' ELSE 'regular' END
              AND timestamp < latest_session.timestamp - INTERVAL '12 hours'
            ORDER BY timestamp DESC
            LIMIT 1
        ) AS previous_session ON TRUE
        LEFT JOIN LATERAL (
            SELECT price
            FROM stock_quotes
            WHERE symbol = $1
            ORDER BY timestamp ASC
            LIMIT 1
        ) AS oldest ON latest_session.timestamp IS NULL
        "#,
    )
    .bind(symbol)
    .fetch_optional(pool)
    .await
    .map_err(internal_error)?;

    let Some(r) = row else {
        return Ok(None);
    };

    let sym: String = r.get("symbol");
    let timestamp: DateTime<Utc> = r.get("timestamp");
    let price: f64 = r.get("price");
    let volume: i64 = r.get("volume");
    let market_session: String = r.get("market_session");
    let reference_price: Option<f64> = r.get("reference_price");

    let quote = MarketQuote {
        symbol: sym.clone(),
        timestamp,
        price,
        volume,
        market_session,
    };

    let delta = reference_price.map(|ref_price| {
        let abs_change = price - ref_price;
        let pct_change = if ref_price != 0.0 {
            abs_change / ref_price * 100.0
        } else {
            0.0
        };
        MarketDelta {
            symbol: sym,
            reference_price: ref_price,
            latest_price: price,
            pct_change,
            abs_change,
        }
    });

    Ok(Some((quote, delta.unwrap_or(MarketDelta {
        symbol: symbol.to_string(),
        reference_price: price,
        latest_price: price,
        pct_change: 0.0,
        abs_change: 0.0,
    }))))
}

async fn tracked_symbols_handler(
    State(state): State<AppState>,
) -> Result<Json<Vec<String>>, (StatusCode, String)> {
    let rows = sqlx::query("SELECT symbol FROM tracked_symbols WHERE is_active ORDER BY symbol")
        .fetch_all(&state.pool)
        .await
        .map_err(internal_error)?;

    let symbols: Vec<String> = rows.into_iter().map(|r| r.get("symbol")).collect();
    Ok(Json(symbols))
}

async fn dashboard_handler(
    State(state): State<AppState>,
    Query(params): Query<DashboardQueryParams>,
) -> Result<Json<DashboardResponse>, (StatusCode, String)> {
    let symbol = params.symbol.unwrap_or_else(|| "SMH".to_string());
    let hours = validated_hours(params.hours)?;
    let platform = params.platform.filter(|value| value != "all");
    let cutoff = Utc::now() - chrono::Duration::hours(hours);

    // 1. Fetch sentiment stats
    let sentiment_rows = sqlx::query(
        r#"
        SELECT sentiment, COUNT(*) as count 
        FROM posts 
        WHERE symbol = $1 AND timestamp >= $2
          AND ($3::TEXT IS NULL OR platform = $3)
        GROUP BY sentiment
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .bind(platform.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let sentiment_stats: Vec<SentimentStats> = sentiment_rows
        .into_iter()
        .map(|r| SentimentStats {
            sentiment: r.get("sentiment"),
            count: r.get("count"),
        })
        .collect();

    // 2. Fetch topic stats
    let topic_rows = sqlx::query(
        r#"
        SELECT topic_label, COUNT(*) as count 
        FROM posts 
        WHERE symbol = $1 AND timestamp >= $2
          AND ($3::TEXT IS NULL OR platform = $3)
        GROUP BY topic_label
        ORDER BY count DESC
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .bind(platform.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let topic_stats: Vec<TopicStats> = topic_rows
        .into_iter()
        .map(|r| TopicStats {
            topic_label: r.get("topic_label"),
            count: r.get("count"),
        })
        .collect();

    // 3. Fetch recent posts
    let post_rows = sqlx::query(
        r#"
        SELECT id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, scored_at, engagement
        FROM posts
        WHERE symbol = $1 AND timestamp >= $2
          AND ($3::TEXT IS NULL OR platform = $3)
        ORDER BY timestamp DESC
        LIMIT 500
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .bind(platform.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let posts: Vec<PostResponse> = post_rows
        .into_iter()
        .map(|r| PostResponse {
            id: r.get("id"),
            symbol: r.get("symbol"),
            platform: r.get("platform"),
            text: r.get("text"),
            timestamp: r.get("timestamp"),
            sentiment: r.get("sentiment"),
            scores: r.get("scores"),
            topic_id: r.get("topic_id"),
            topic_label: r.get("topic_label"),
            scored_at: r.get("scored_at"),
            engagement: r.get("engagement"),
        })
        .collect();

    // 4. Fetch market quotes
    let market_data = fetch_market_series(&state.pool, &symbol, cutoff).await?;
    let (latest_quote, primary_delta) = fetch_market_snapshot(&state.pool, &symbol)
        .await?
        .unzip();

    // 5. Fetch stock metrics
    let metrics_row = sqlx::query(
        r#"
        SELECT symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y,
               pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at
        FROM stock_metrics
        WHERE symbol = $1
        "#,
    )
    .bind(&symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?;

    let metrics_data = metrics_row.map(|r| StockMetricsResponse {
        symbol: r.get("symbol"),
        pe_ratio: r.get("pe_ratio"),
        beta: r.get("beta"),
        avg_return_1y: r.get("avg_return_1y"),
        inflation_adj_return_1y: r.get("inflation_adj_return_1y"),
        pe_relative_sector: r.get("pe_relative_sector"),
        beta_relative_sector: r.get("beta_relative_sector"),
        return_relative_sector: r.get("return_relative_sector"),
        updated_at: r.get("updated_at"),
    });

    let primary_future_symbol = sqlx::query_scalar::<_, Option<String>>(
        "SELECT future FROM tracked_symbols WHERE symbol = $1 AND is_active",
    )
    .bind(&symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?
    .flatten();
    let (primary_future_quote, primary_future_delta, primary_future_market_data) =
        if let Some(future_symbol) = primary_future_symbol.as_deref() {
            let data = fetch_market_series(&state.pool, future_symbol, cutoff).await?;
            let (q, d) = fetch_market_snapshot(&state.pool, future_symbol).await?.unzip();
            (q, d, data)
        } else {
            (None, None, Vec::new())
        };
    let (vix_quote, vix_delta) = fetch_market_snapshot(&state.pool, &state.vix_symbol)
        .await?
        .unzip();

    Ok(Json(DashboardResponse {
        sentiment_stats,
        topic_stats,
        posts,
        market_data,
        latest_quote,
        metrics_data,
        primary_delta,
        primary_future_symbol,
        primary_future_quote,
        primary_future_delta,
        primary_future_market_data,
        vix_quote,
        vix_delta,
    }))
}

async fn leaderboard_handler(
    State(state): State<AppState>,
) -> Result<Json<Vec<LeaderboardEntry>>, (StatusCode, String)> {
    let rows = sqlx::query(
        r#"
        WITH current_stats AS (
            SELECT symbol,
                   COUNT(*) AS post_count_4h,
                   (COUNT(*) FILTER (WHERE sentiment = 'positive')
                    - COUNT(*) FILTER (WHERE sentiment = 'negative'))::DOUBLE PRECISION
                     / NULLIF(COUNT(*), 0) AS sentiment_index_4h
            FROM posts
            WHERE timestamp >= NOW() - INTERVAL '4 hours'
            GROUP BY symbol
        ),
        hourly_counts AS (
            SELECT symbol, date_trunc('hour', timestamp) AS bucket_hour,
                   COUNT(*)::DOUBLE PRECISION AS post_count
            FROM posts
            WHERE timestamp >= NOW() - INTERVAL '28 days'
              AND timestamp < NOW() - INTERVAL '4 hours'
            GROUP BY symbol, date_trunc('hour', timestamp)
        ),
        baseline AS (
            SELECT symbol,
                   AVG(post_count) AS mean_hourly,
                   STDDEV_SAMP(post_count) AS stddev_hourly,
                   COUNT(*) AS sample_count
            FROM hourly_counts
            GROUP BY symbol
        )
        SELECT universe.symbol,
               COALESCE(current_stats.post_count_4h, 0) AS post_count_4h,
               COALESCE(current_stats.sentiment_index_4h, 0.0) AS sentiment_index_4h,
               CASE
                   WHEN baseline.sample_count < 8 OR baseline.stddev_hourly IS NULL
                        OR baseline.stddev_hourly = 0 THEN NULL
                   ELSE ((COALESCE(current_stats.post_count_4h, 0) / 4.0)
                         - baseline.mean_hourly) / baseline.stddev_hourly
               END AS buzz_z,
               COALESCE(baseline.mean_hourly, 0.0) AS baseline_hourly,
               COALESCE(baseline.sample_count, 0) AS baseline_samples
        FROM tracked_symbols AS universe
        LEFT JOIN current_stats USING (symbol)
        LEFT JOIN baseline USING (symbol)
        WHERE universe.is_active
        ORDER BY buzz_z DESC NULLS LAST, post_count_4h DESC, universe.symbol
        "#,
    )
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let entries = rows
        .into_iter()
        .map(|row| LeaderboardEntry {
            symbol: row.get("symbol"),
            post_count_4h: row.get("post_count_4h"),
            sentiment_index_4h: row.get("sentiment_index_4h"),
            buzz_z: row.get("buzz_z"),
            baseline_hourly: row.get("baseline_hourly"),
            baseline_samples: row.get("baseline_samples"),
        })
        .collect();
    Ok(Json(entries))
}

fn source_status(posts_1h: i64, posts_24h: i64, age_seconds: Option<f64>) -> String {
    if posts_24h == 0 {
        return "silent".to_string();
    }
    let expected_gap = 86_400.0 / posts_24h as f64;
    if age_seconds.is_some_and(|age| age > 1_800.0 && age > 12.0 * expected_gap) {
        "stalled".to_string()
    } else if posts_1h > 0 {
        "active".to_string()
    } else {
        "quiet".to_string()
    }
}

async fn sources_handler(
    State(state): State<AppState>,
) -> Result<Json<Vec<SourceHealth>>, (StatusCode, String)> {
    let platforms = vec!["bluesky", "stocktwits", "finnhub", "alpaca"];
    let rows = sqlx::query(
        r#"
        SELECT platform,
               COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '1 hour') AS posts_1h,
               COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') AS posts_24h,
               MAX(timestamp) AS last_ingest
        FROM posts
        WHERE platform = ANY($1) AND timestamp > NOW() - INTERVAL '7 days'
        GROUP BY platform
        "#,
    )
    .bind(&platforms)
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let mut by_platform = std::collections::HashMap::new();
    for row in rows {
        by_platform.insert(
            row.get::<String, _>("platform"),
            (
                row.get::<i64, _>("posts_1h"),
                row.get::<i64, _>("posts_24h"),
                row.get::<Option<DateTime<Utc>>, _>("last_ingest"),
            ),
        );
    }

    let now = Utc::now();
    let mut health = platforms
        .into_iter()
        .map(|platform| {
            let (posts_1h, posts_24h, last_ingest) =
                by_platform.remove(platform).unwrap_or((0, 0, None));
            let age_seconds =
                last_ingest.map(|timestamp| (now - timestamp).num_milliseconds() as f64 / 1_000.0);
            SourceHealth {
                platform: platform.to_string(),
                posts_1h,
                posts_24h,
                last_ingest,
                age_seconds,
                baseline_per_hour: (posts_24h > 0).then_some(posts_24h as f64 / 24.0),
                status: source_status(posts_1h, posts_24h, age_seconds),
            }
        })
        .collect::<Vec<_>>();

    let market_row = sqlx::query(
        r#"
        SELECT COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '1 hour') AS posts_1h,
               COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') AS posts_24h,
               MAX(timestamp) AS last_ingest,
               (array_agg(market_session ORDER BY timestamp DESC))[1] AS market_session
        FROM stock_quotes
        WHERE timestamp > NOW() - INTERVAL '7 days'
        "#,
    )
    .fetch_one(&state.pool)
    .await
    .map_err(internal_error)?;
    let posts_1h: i64 = market_row.get("posts_1h");
    let posts_24h: i64 = market_row.get("posts_24h");
    let last_ingest: Option<DateTime<Utc>> = market_row.get("last_ingest");
    let age_seconds =
        last_ingest.map(|timestamp| (now - timestamp).num_milliseconds() as f64 / 1_000.0);
    let mut status = source_status(posts_1h, posts_24h, age_seconds);
    let market_session: Option<String> = market_row.get("market_session");
    if market_session.as_deref() != Some("regular")
        && matches!(status.as_str(), "stalled" | "silent")
    {
        status = "quiet".to_string();
    }
    health.push(SourceHealth {
        platform: "yfinance".to_string(),
        posts_1h,
        posts_24h,
        last_ingest,
        age_seconds,
        baseline_per_hour: (posts_24h > 0).then_some(posts_24h as f64 / 24.0),
        status,
    });

    health.sort_by_key(|source| match source.status.as_str() {
        "silent" => 0,
        "stalled" => 1,
        "quiet" => 2,
        _ => 3,
    });

    Ok(Json(health))
}

async fn sentiment_stats_handler(
    State(state): State<AppState>,
    Query(params): Query<DashboardQueryParams>,
) -> Result<Json<Vec<SentimentStats>>, (StatusCode, String)> {
    let cutoff = Utc::now() - chrono::Duration::hours(validated_hours(params.hours)?);
    let platform = params.platform.filter(|value| value != "all");
    let rows = sqlx::query(
        r#"
        SELECT sentiment, COUNT(*) AS count
        FROM posts
        WHERE timestamp > $1
          AND ($2::TEXT IS NULL OR symbol = $2)
          AND ($3::TEXT IS NULL OR platform = $3)
        GROUP BY sentiment
        "#,
    )
    .bind(cutoff)
    .bind(params.symbol.as_deref())
    .bind(platform.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;
    Ok(Json(
        rows.into_iter()
            .map(|row| SentimentStats {
                sentiment: row.get("sentiment"),
                count: row.get("count"),
            })
            .collect(),
    ))
}

async fn topic_stats_handler(
    State(state): State<AppState>,
    Query(params): Query<DashboardQueryParams>,
) -> Result<Json<Vec<TopicStats>>, (StatusCode, String)> {
    let cutoff = Utc::now() - chrono::Duration::hours(validated_hours(params.hours)?);
    let platform = params.platform.filter(|value| value != "all");
    let rows = sqlx::query(
        r#"
        SELECT topic_label, COUNT(*) AS count
        FROM posts
        WHERE timestamp > $1
          AND ($2::TEXT IS NULL OR symbol = $2)
          AND ($3::TEXT IS NULL OR platform = $3)
        GROUP BY topic_label
        ORDER BY count DESC
        "#,
    )
    .bind(cutoff)
    .bind(params.symbol.as_deref())
    .bind(platform.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;
    Ok(Json(
        rows.into_iter()
            .map(|row| TopicStats {
                topic_label: row.get("topic_label"),
                count: row.get("count"),
            })
            .collect(),
    ))
}

async fn market_handler(
    State(state): State<AppState>,
    Query(params): Query<MarketQueryParams>,
) -> Result<Json<Vec<MarketQuote>>, (StatusCode, String)> {
    let cutoff = Utc::now() - chrono::Duration::hours(validated_hours(params.hours)?);
    Ok(Json(
        fetch_market_series(&state.pool, &params.symbol, cutoff).await?,
    ))
}

async fn latest_market_handler(
    State(state): State<AppState>,
    Query(params): Query<SymbolQueryParams>,
) -> Result<Json<Option<MarketQuote>>, (StatusCode, String)> {
    let row = sqlx::query(
        r#"
        SELECT symbol, timestamp, price, volume, market_session
        FROM stock_quotes
        WHERE symbol = $1
        ORDER BY timestamp DESC
        LIMIT 1
        "#,
    )
    .bind(&params.symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?;
    Ok(Json(row.map(|row| MarketQuote {
        symbol: row.get("symbol"),
        timestamp: row.get("timestamp"),
        price: row.get("price"),
        volume: row.get("volume"),
        market_session: row.get("market_session"),
    })))
}

async fn market_delta_handler(
    State(state): State<AppState>,
    Query(params): Query<MarketDeltaQueryParams>,
) -> Result<Json<Option<MarketDelta>>, (StatusCode, String)> {
    let latest = sqlx::query_scalar::<_, f64>(
        "SELECT price FROM stock_quotes WHERE symbol = $1 ORDER BY timestamp DESC LIMIT 1",
    )
    .bind(&params.symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?;
    let Some(latest_price) = latest else {
        return Ok(Json(None));
    };
    let mut reference = sqlx::query_scalar::<_, f64>(
        r#"
        SELECT price FROM stock_quotes
        WHERE symbol = $1 AND timestamp <= $2
        ORDER BY timestamp DESC LIMIT 1
        "#,
    )
    .bind(&params.symbol)
    .bind(params.since)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?;
    if reference.is_none() {
        reference = sqlx::query_scalar::<_, f64>(
            "SELECT price FROM stock_quotes WHERE symbol = $1 ORDER BY timestamp LIMIT 1",
        )
        .bind(&params.symbol)
        .fetch_optional(&state.pool)
        .await
        .map_err(internal_error)?;
    }
    let Some(reference_price) = reference else {
        return Ok(Json(None));
    };
    let abs_change = latest_price - reference_price;
    let pct_change = if reference_price == 0.0 {
        0.0
    } else {
        abs_change / reference_price * 100.0
    };
    Ok(Json(Some(MarketDelta {
        symbol: params.symbol,
        reference_price,
        latest_price,
        pct_change,
        abs_change,
    })))
}

async fn stock_metrics_handler(
    State(state): State<AppState>,
    Query(params): Query<SymbolQueryParams>,
) -> Result<Json<Option<StockMetricsResponse>>, (StatusCode, String)> {
    let row = sqlx::query(
        r#"
        SELECT symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y,
               pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at
        FROM stock_metrics WHERE symbol = $1
        "#,
    )
    .bind(&params.symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?;
    Ok(Json(row.map(|row| StockMetricsResponse {
        symbol: row.get("symbol"),
        pe_ratio: row.get("pe_ratio"),
        beta: row.get("beta"),
        avg_return_1y: row.get("avg_return_1y"),
        inflation_adj_return_1y: row.get("inflation_adj_return_1y"),
        pe_relative_sector: row.get("pe_relative_sector"),
        beta_relative_sector: row.get("beta_relative_sector"),
        return_relative_sector: row.get("return_relative_sector"),
        updated_at: row.get("updated_at"),
    })))
}

async fn posts_handler(
    State(state): State<AppState>,
    Query(params): Query<PostsQueryParams>,
) -> Result<Json<Vec<PostResponse>>, (StatusCode, String)> {
    let limit = params.limit.unwrap_or(20);
    let offset = params.offset.unwrap_or(0);
    if !(1..=1_000).contains(&limit) {
        return Err((
            StatusCode::BAD_REQUEST,
            "limit must be between 1 and 1000".to_string(),
        ));
    }
    if offset < 0 {
        return Err((
            StatusCode::BAD_REQUEST,
            "offset must be non-negative".to_string(),
        ));
    }

    let platform = params.platform.filter(|value| value != "all");
    let sentiment = params.sentiment.filter(|value| value != "all");
    if sentiment
        .as_deref()
        .is_some_and(|value| !matches!(value, "positive" | "neutral" | "negative"))
    {
        return Err((
            StatusCode::BAD_REQUEST,
            "sentiment must be positive, neutral, or negative".to_string(),
        ));
    }
    let hour = params
        .hour
        .as_deref()
        .map(DateTime::parse_from_rfc3339)
        .transpose()
        .map_err(|_| {
            (
                StatusCode::BAD_REQUEST,
                "hour must be an RFC 3339 timestamp".to_string(),
            )
        })?
        .map(|value| value.with_timezone(&Utc));

    let query_str = r#"
        SELECT id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, scored_at, engagement
        FROM posts
        WHERE ($1::TEXT IS NULL OR symbol = $1)
          AND ($2::TEXT IS NULL OR platform = $2)
          AND ($3::TEXT IS NULL OR sentiment = $3)
          AND ($4::TEXT IS NULL OR topic_label = $4)
          AND ($5::TIMESTAMPTZ IS NULL OR date_trunc('hour', timestamp) = date_trunc('hour', $5))
          AND ($6::TIMESTAMPTZ IS NULL OR timestamp >= $6)
          AND ($7::TIMESTAMPTZ IS NULL OR timestamp < $7)
        ORDER BY timestamp DESC
        LIMIT $8 OFFSET $9
    "#;

    let rows = sqlx::query(query_str)
        .bind(params.symbol.as_deref())
        .bind(platform.as_deref())
        .bind(sentiment.as_deref())
        .bind(params.topic.as_deref())
        .bind(hour)
        .bind(params.start_time)
        .bind(params.end_time)
        .bind(limit)
        .bind(offset)
        .fetch_all(&state.pool)
        .await
        .map_err(internal_error)?;

    let posts = rows
        .into_iter()
        .map(|r| PostResponse {
            id: r.get("id"),
            symbol: r.get("symbol"),
            platform: r.get("platform"),
            text: r.get("text"),
            timestamp: r.get("timestamp"),
            sentiment: r.get("sentiment"),
            scores: r.get("scores"),
            topic_id: r.get("topic_id"),
            topic_label: r.get("topic_label"),
            scored_at: r.get("scored_at"),
            engagement: r.get("engagement"),
        })
        .collect();

    Ok(Json(posts))
}

fn pearson(pairs: &[(f64, f64)]) -> Option<f64> {
    if pairs.len() < 3 {
        return None;
    }
    let count = pairs.len() as f64;
    let mean_x = pairs.iter().map(|(x, _)| x).sum::<f64>() / count;
    let mean_y = pairs.iter().map(|(_, y)| y).sum::<f64>() / count;
    let numerator = pairs
        .iter()
        .map(|(x, y)| (x - mean_x) * (y - mean_y))
        .sum::<f64>();
    let sum_x = pairs.iter().map(|(x, _)| (x - mean_x).powi(2)).sum::<f64>();
    let sum_y = pairs.iter().map(|(_, y)| (y - mean_y).powi(2)).sum::<f64>();
    let denominator = (sum_x * sum_y).sqrt();
    (denominator > f64::EPSILON).then_some(numerator / denominator)
}

struct MacdPoint {
    macd: Option<f64>,
    signal: Option<f64>,
    hist: Option<f64>,
}

fn sma(seq: &[Option<f64>], i: usize, period: usize) -> Option<f64> {
    if i + 1 < period {
        return None;
    }
    let window = &seq[i + 1 - period..=i];
    let mut sum = 0.0;
    for &val in window {
        match val {
            Some(v) => sum += v,
            None => return None,
        }
    }
    Some(sum / period as f64)
}

fn compute_sentiment_macd(
    indices: &[Option<f64>],
    fast: usize,
    slow: usize,
    signal: usize,
) -> Vec<MacdPoint> {
    let n = indices.len();
    if n == 0 {
        return Vec::new();
    }
    let mut filled = Vec::with_capacity(n);
    let mut last = None;
    for &v in indices {
        if v.is_some() {
            last = v;
        }
        filled.push(last);
    }

    let mut macd_line = Vec::with_capacity(n);
    for i in 0..n {
        let f = sma(&filled, i, fast);
        let s = sma(&filled, i, slow);
        macd_line.push(match (f, s) {
            (Some(f_val), Some(s_val)) => Some(f_val - s_val),
            _ => None,
        });
    }

    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let sig = sma(&macd_line, i, signal);
        let m = macd_line[i];
        let hist = match (m, sig) {
            (Some(m_val), Some(sig_val)) => Some(m_val - sig_val),
            _ => None,
        };
        out.push(MacdPoint {
            macd: m,
            signal: sig,
            hist,
        });
    }
    out
}

struct OpportunityResult {
    score: f64,
    classification: &'static str,
    color: &'static str,
    strategy: &'static str,
    description: &'static str,
    checklist: Vec<String>,
}

fn compute_opportunity(
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
    let mut checklist = Vec::new();

    // 1. Price near support
    if let Some(p) = price {
        if support > 0.0 && p > 0.0 {
            let dist_to_support = (p - support) / p;
            if dist_to_support <= 0.025 {
                score += 3.0;
                checklist.push("Price near support level".to_string());
            } else if dist_to_support <= 0.05 {
                score += 1.5;
                checklist.push("Price approaching support level".to_string());
            }
        }
    }

    // 2. Sentiment momentum (crossover)
    if sentiment > sentiment_sma {
        if sentiment_sma > 0.0 {
            score += 3.0;
            checklist.push("Bullish sentiment crossover (above positive SMA)".to_string());
        } else {
            score += 1.5;
            checklist.push("Bullish sentiment crossover (above negative SMA)".to_string());
        }
    }

    // 3. Sentiment-price divergence (price down, sentiment up in recent trailing window)
    if prev_prices.len() >= 3 && prev_sentiments.len() >= 3 {
        let price_trend = prev_prices.last().unwrap() - prev_prices.first().unwrap();
        let sentiment_trend = prev_sentiments.last().unwrap() - prev_sentiments.first().unwrap();
        if price_trend < 0.0 && sentiment_trend > 0.1 {
            score += 2.0;
            checklist.push("Bullish sentiment divergence (price down, sentiment rising)".to_string());
        }
    }

    // 4. Valuation vs sector benchmark
    if let Some(pe_rel) = pe_relative {
        if pe_rel < 0.0 {
            score += 2.0;
            checklist.push("Undervalued relative to sector benchmarks".to_string());
        } else if pe_rel == 0.0 {
            score += 1.0;
            checklist.push("Fairly valued relative to sector benchmarks".to_string());
        }
    }

    let opportunity_pct = (score / 10.0 * 100.0).min(100.0);

    // Classification
    let (classification, color) = if opportunity_pct >= 75.0 {
        ("STRONG BUY", "emerald")
    } else if opportunity_pct >= 50.0 {
        ("ACCUMULATE", "teal")
    } else if opportunity_pct >= 30.0 {
        ("HOLD / NEUTRAL", "slate")
    } else {
        ("CAUTION / OVERBOUGHT", "rose")
    };

    // Strategy Recommendation
    let mut strategy = "Hold / Sell Covered Calls";
    let mut strategy_desc = "Neutral market stance. Capitalize on range-bound behavior using covered call writing.";

    let vix = vix_price.unwrap_or(15.0);

    if classification == "STRONG BUY" || classification == "ACCUMULATE" {
        if vix < 15.0 {
            strategy = "Long Call Option (Buy Call)";
            strategy_desc = "Low volatility regime. Buy outright call options to leverage cheap premium.";
        } else if vix <= 22.0 {
            strategy = "Long Shares / Bull Call Spread";
            strategy_desc = "Moderate volatility. Buy shares directly or use bull call debit spreads.";
        } else {
            strategy = "Sell Put Credit Spreads";
            strategy_desc = "High volatility regime. Sell put credit spreads to harvest rich premium.";
        }
    } else if classification == "CAUTION / OVERBOUGHT" {
        if vix > 22.0 {
            strategy = "Sell Call Credit Spreads";
            strategy_desc = "High volatility overbought. Sell call credit spreads for fast premium decay.";
        } else {
            strategy = "Protect Longs / Buy Puts";
            strategy_desc = "Overbought stance. Trim shares or buy puts to hedge downside risk.";
        }
    }

    OpportunityResult {
        score: opportunity_pct,
        classification,
        color,
        strategy,
        description: strategy_desc,
        checklist,
    }
}

fn lagged_correlations(buckets: &[CorrelationBucket]) -> (f64, i32, Vec<LagSweepValue>) {
    let mut max_r: f64 = 0.0;
    let mut best_lag = 0;
    let mut sweeps = Vec::with_capacity(11);
    for lag in -5_i32..=5 {
        let mut pairs = Vec::new();
        for (index, sentiment_bucket) in buckets.iter().enumerate() {
            let price_index = index as isize + lag as isize;
            if price_index < 0 || price_index >= buckets.len() as isize {
                continue;
            }
            let price_bucket = &buckets[price_index as usize];
            if let Some(price_change) = price_bucket.price_change {
                if price_bucket.is_market_open {
                    pairs.push((sentiment_bucket.sentiment_index, price_change));
                }
            }
        }
        let correlation = if pairs.len() < 4 {
            0.0
        } else {
            pearson(&pairs).unwrap_or(0.0)
        };
        sweeps.push(LagSweepValue {
            lag,
            r: correlation,
        });
        if correlation.abs() > max_r.abs() {
            max_r = correlation;
            best_lag = lag;
        }
    }
    (max_r, best_lag, sweeps)
}

fn percentile(sorted: &[f64], fraction: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let index = ((sorted.len() - 1) as f64 * fraction).floor() as usize;
    sorted[index]
}

async fn correlation_handler(
    State(state): State<AppState>,
    Query(params): Query<CorrelationQueryParams>,
) -> Result<Json<CorrelationResponse>, (StatusCode, String)> {
    let symbol = params.symbol.unwrap_or_else(|| "SMH".to_string());
    let hours = validated_hours(params.hours)?;
    let platform = params.platform.filter(|value| value != "all");
    let topic = params.topic.filter(|value| value != "all");
    let is_filtered = platform.is_some() || topic.is_some();
    let now = Utc::now();
    let cutoff = (now - chrono::Duration::hours(hours))
        .with_minute(0)
        .and_then(|v| v.with_second(0))
        .and_then(|v| v.with_nanosecond(0))
        .ok_or_else(|| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to construct cutoff timeline".to_string(),
            )
        })?;

    let primary_future_symbol = sqlx::query_scalar::<_, Option<String>>(
        "SELECT future FROM tracked_symbols WHERE symbol = $1 AND is_active",
    )
    .bind(&symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?
    .flatten();

    let current_hour = now
        .with_minute(0)
        .and_then(|value| value.with_second(0))
        .and_then(|value| value.with_nanosecond(0))
        .ok_or_else(|| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to construct hourly timeline".to_string(),
            )
        })?;

    let mut buckets: Vec<CorrelationBucket> = Vec::with_capacity(hours as usize + 1);
    let mut bucket_indices: std::collections::HashMap<DateTime<Utc>, usize> =
        std::collections::HashMap::with_capacity(hours as usize + 1);
    let mut total_weighted: Vec<f64> = vec![0.0; hours as usize + 1];
    let mut weighted_signal: Vec<f64> = vec![0.0; hours as usize + 1];

    for offset in (0..=hours).rev() {
        let bucket_time = current_hour - chrono::Duration::hours(offset);
        let ts_str = bucket_time.to_rfc3339().replace("+00:00", "Z");
        let idx = buckets.len();
        bucket_indices.insert(bucket_time, idx);
        buckets.push(CorrelationBucket {
            timestamp: ts_str,
            positive: 0,
            neutral: 0,
            negative: 0,
            price_change: None,
            price_pct: None,
            future_change: None,
            future_pct: None,
            is_market_open: false,
            sentiment_index: 0.0,
            sentiment_sma: 0.0,
            raw_price: None,
            buy_signal: None,
            signal_quality: None,
            buy_score: None,
            support_price: None,
            support_pct: None,
            resistance_price: None,
            resistance_pct: None,
            sentiment_macd: None,
            sentiment_signal: None,
            sentiment_hist: None,
        });
    }

    // 1. Resample target and future quotes to UTC-hour closes
    let db_cutoff = cutoff - chrono::Duration::hours(1);
    let mut market_symbols = vec![symbol.clone()];
    if let Some(ref fut) = primary_future_symbol {
        if fut != &symbol {
            market_symbols.push(fut.clone());
        }
    }

    struct HourlyMarketRow {
        bucket_hour: DateTime<Utc>,
        price: f64,
        price_change: Option<f64>,
    }

    let hourly_market_rows = sqlx::query(
        r#"
        WITH hourly_sources AS (
            SELECT
                symbol,
                bucket_hour,
                close_price,
                had_regular_session AS is_market_open,
                last_observed_at
            FROM market_hourly_facts
            WHERE symbol = ANY($1) AND bucket_hour >= $2

            UNION ALL

            SELECT
                symbol,
                date_trunc('hour', timestamp, 'UTC') AS bucket_hour,
                (array_agg(price ORDER BY timestamp DESC))[1] AS close_price,
                bool_or(market_session = 'regular') AS is_market_open,
                MAX(timestamp) AS last_observed_at
            FROM stock_quotes
            WHERE symbol = ANY($1) AND timestamp >= $2
            GROUP BY symbol, date_trunc('hour', timestamp, 'UTC')
        ),
        hourly_closes AS (
            SELECT
                symbol,
                bucket_hour,
                (array_agg(close_price ORDER BY last_observed_at DESC))[1]
                    AS close_price,
                bool_or(is_market_open) AS is_market_open
            FROM hourly_sources
            GROUP BY symbol, bucket_hour
        ),
        hourly_with_previous AS (
            SELECT
                symbol,
                bucket_hour,
                close_price,
                is_market_open,
                lag(bucket_hour) OVER (
                    PARTITION BY symbol ORDER BY bucket_hour
                ) AS previous_hour,
                lag(close_price) OVER (
                    PARTITION BY symbol ORDER BY bucket_hour
                ) AS previous_close
            FROM hourly_closes
        )
        SELECT
            symbol,
            bucket_hour,
            close_price AS price,
            CASE
                WHEN previous_hour = bucket_hour - INTERVAL '1 hour'
                     AND previous_close <> 0
                THEN ((close_price - previous_close) / previous_close) * 100.0
                ELSE NULL
            END AS price_change,
            is_market_open
        FROM hourly_with_previous
        ORDER BY symbol, bucket_hour
        "#,
    )
    .bind(&market_symbols)
    .bind(db_cutoff)
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let mut target_market_data = Vec::new();
    let mut future_market_data = Vec::new();
    for row in hourly_market_rows {
        let sym: String = row.get("symbol");
        let b_hour: DateTime<Utc> = row.get("bucket_hour");
        let price: f64 = row.get("price");
        let price_change: Option<f64> = row.get("price_change");
        let is_market_open: bool = row.get("is_market_open");

        if sym == symbol {
            target_market_data.push(HourlyMarketRow {
                bucket_hour: b_hour,
                price,
                price_change,
            });
            let b_hour_aligned = b_hour.with_minute(0).and_then(|v| v.with_second(0)).and_then(|v| v.with_nanosecond(0)).unwrap_or(b_hour);
            if let Some(&idx) = bucket_indices.get(&b_hour_aligned) {
                buckets[idx].price_change = price_change;
                buckets[idx].raw_price = Some(price);
                buckets[idx].is_market_open = is_market_open;
            }
        } else if primary_future_symbol.as_deref() == Some(&sym) {
            future_market_data.push(HourlyMarketRow {
                bucket_hour: b_hour,
                price,
                price_change,
            });
        }
    }

    let latest_future_price = future_market_data.last().map(|row| row.price);
    for row in future_market_data {
        let b_hour_aligned = row.bucket_hour.with_minute(0).and_then(|v| v.with_second(0)).and_then(|v| v.with_nanosecond(0)).unwrap_or(row.bucket_hour);
        if let Some(&idx) = bucket_indices.get(&b_hour_aligned) {
            buckets[idx].future_change = row.price_change;
            if let Some(lfp) = latest_future_price.filter(|&p| p != 0.0) {
                buckets[idx].future_pct = Some((row.price - lfp) / lfp * 100.0);
            }
        }
    }

    // 2. Canonical facts and legacy aggregates
    let agg_rows = sqlx::query(
        r#"
        WITH canonical AS (
            SELECT
                bucket_hour,
                SUM(positive_count)::bigint AS positive_count,
                SUM(neutral_count)::bigint AS neutral_count,
                SUM(negative_count)::bigint AS negative_count,
                COALESCE(SUM(positive_weight_sum), 0.0)::double precision AS positive_weighted,
                COALESCE(SUM(negative_weight_sum), 0.0)::double precision AS negative_weighted,
                COALESCE(SUM(neutral_weight_sum), 0.0)::double precision AS neutral_weighted,
                COALESCE(SUM(weight_sum), 0.0)::double precision AS total_weighted,
                COALESCE(SUM(weighted_signal_sum), 0.0)::double precision AS weighted_signal_sum
            FROM hourly_sentiment_facts
            WHERE symbol = $1 AND bucket_hour >= $2
              AND ($3::TEXT IS NULL OR platform = $3)
              AND ($4::TEXT IS NULL OR topic_label = $4)
            GROUP BY bucket_hour
        ),
        legacy AS (
            SELECT
                legacy.bucket_hour,
                legacy.positive_count::bigint AS positive_count,
                legacy.neutral_count::bigint AS neutral_count,
                legacy.negative_count::bigint AS negative_count,
                legacy.positive_weighted::double precision AS positive_weighted,
                legacy.negative_weighted::double precision AS negative_weighted,
                legacy.neutral_weighted::double precision AS neutral_weighted,
                legacy.total_weighted::double precision AS total_weighted,
                (legacy.positive_weighted - legacy.negative_weighted)::double precision
                    AS weighted_signal_sum
            FROM hourly_sentiment_agg AS legacy
            WHERE legacy.symbol = $1
              AND legacy.bucket_hour >= $2
              AND $5::BOOLEAN = FALSE
              AND NOT EXISTS (
                  SELECT 1
                  FROM canonical
                  WHERE canonical.bucket_hour = legacy.bucket_hour
              )
        )
        SELECT
            bucket_hour,
            positive_count,
            neutral_count,
            negative_count,
            positive_weighted,
            negative_weighted,
            neutral_weighted,
            total_weighted,
            weighted_signal_sum,
            (weighted_signal_sum / NULLIF(total_weighted, 0))::double precision AS sentiment_index
        FROM canonical
        UNION ALL
        SELECT
            bucket_hour,
            positive_count,
            neutral_count,
            negative_count,
            positive_weighted,
            negative_weighted,
            neutral_weighted,
            total_weighted,
            weighted_signal_sum,
            (weighted_signal_sum / NULLIF(total_weighted, 0))::double precision AS sentiment_index
        FROM legacy
        ORDER BY bucket_hour
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .bind(platform.as_deref())
    .bind(topic.as_deref())
    .bind(is_filtered)
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    for row in agg_rows {
        let b_hour: DateTime<Utc> = row.get("bucket_hour");
        let b_hour_aligned = b_hour.with_minute(0).and_then(|v| v.with_second(0)).and_then(|v| v.with_nanosecond(0)).unwrap_or(b_hour);
        if let Some(&idx) = bucket_indices.get(&b_hour_aligned) {
            buckets[idx].positive = row.get::<i64, _>("positive_count");
            buckets[idx].neutral = row.get::<i64, _>("neutral_count");
            buckets[idx].negative = row.get::<i64, _>("negative_count");
            let tw: f64 = row.get("total_weighted");
            let ws: f64 = row.get("weighted_signal_sum");
            total_weighted[idx] = tw;
            weighted_signal[idx] = ws;
            buckets[idx].sentiment_index = row.get::<Option<f64>, _>("sentiment_index").unwrap_or(0.0);
        }
    }

    // 3. Overlay with live posts data (hot tier)
    let post_rows = sqlx::query(
        r#"
        SELECT sentiment, scores, timestamp, engagement
        FROM posts
        WHERE symbol = $1 AND timestamp >= $2
          AND ($3::TEXT IS NULL OR platform = $3)
          AND ($4::TEXT IS NULL OR topic_label = $4)
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .bind(platform.as_deref())
    .bind(topic.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    for p in post_rows {
        let p_ts: DateTime<Utc> = p.get("timestamp");
        let p_ts_aligned = p_ts.with_minute(0).and_then(|v| v.with_second(0)).and_then(|v| v.with_nanosecond(0)).unwrap_or(p_ts);
        if let Some(&idx) = bucket_indices.get(&p_ts_aligned) {
            let engagement: i32 = p.get::<Option<i32>, _>("engagement").unwrap_or(1).max(0);
            let weight = (engagement as f64).ln_1p();
            let sent: String = p.get("sentiment");
            if sent == "positive" {
                buckets[idx].positive += 1;
            } else if sent == "negative" {
                buckets[idx].negative += 1;
            } else {
                buckets[idx].neutral += 1;
            }
            total_weighted[idx] += weight;

            let scores: Option<serde_json::Value> = p.get("scores");
            let pos_prob = scores.as_ref()
                .and_then(|s| s.get("positive"))
                .and_then(|v| v.as_f64())
                .unwrap_or(if sent == "positive" { 1.0 } else { 0.0 });
            let neg_prob = scores.as_ref()
                .and_then(|s| s.get("negative"))
                .and_then(|v| v.as_f64())
                .unwrap_or(if sent == "negative" { 1.0 } else { 0.0 });

            weighted_signal[idx] += weight * (pos_prob - neg_prob);
        }
    }

    // Post-process sentiment indices
    for i in 0..buckets.len() {
        if total_weighted[i] > 0.0 {
            buckets[i].sentiment_index = weighted_signal[i] / total_weighted[i];
        } else {
            buckets[i].sentiment_index = 0.0;
        }
    }

    // 4. Calculate SMA
    let sma_period = match hours {
        0..=1 => 3,
        2..=24 => 5,
        25..=168 => 12,
        169..=720 => 24,
        _ => 52,
    };
    for i in 0..buckets.len() {
        let mut sum_sma = 0.0;
        let mut count_sma = 0;
        let start_idx = (i + 1).saturating_sub(sma_period);
        for j in start_idx..=i {
            sum_sma += buckets[j].sentiment_index;
            count_sma += 1;
        }
        buckets[i].sentiment_sma = if count_sma > 0 { sum_sma / count_sma as f64 } else { 0.0 };
    }

    // 5. Sentiment MACD
    let (slow_period, signal_period) = match hours {
        0..=1 => (6, 3),
        2..=24 => (12, 5),
        25..=168 => (26, 9),
        _ => (52, 12),
    };
    let macd_indices: Vec<Option<f64>> = buckets
        .iter()
        .enumerate()
        .map(|(i, b)| if total_weighted[i] > 0.0 { Some(b.sentiment_index) } else { None })
        .collect();
    let macd_series = compute_sentiment_macd(&macd_indices, sma_period, slow_period, signal_period);
    for (i, m) in macd_series.into_iter().enumerate() {
        buckets[i].sentiment_macd = m.macd;
        buckets[i].sentiment_signal = m.signal;
        buckets[i].sentiment_hist = m.hist;
    }

    // 6. Calculate Pearson correlation with lag sweeps
    let (max_r, best_lag, lag_sweeps) = lagged_correlations(&buckets);
    let correlation_strength = if max_r.abs() > 0.6 {
        "strong"
    } else if max_r.abs() > 0.35 {
        "moderate"
    } else {
        "weak"
    };
    let sign = if max_r >= 0.0 { "+" } else { "" };
    let correlation_text = if max_r.abs() >= 0.15 {
        let relationship = if max_r >= 0.0 { "positive" } else { "negative" };
        if best_lag > 0 {
            format!("Sentiment leads price by {best_lag}h ({relationship}, r = {sign}{max_r:.2})")
        } else if best_lag < 0 {
            format!("Price leads sentiment by {}h ({relationship}, r = {sign}{max_r:.2})", best_lag.abs())
        } else {
            format!("Coincident correlation ({relationship}, r = {sign}{max_r:.2})")
        }
    } else {
        format!("Weak or no correlation (r = {sign}{max_r:.2})")
    };

    // 7. Closed regions
    let latest_market_session = sqlx::query_scalar::<_, Option<String>>(
        "SELECT market_session FROM stock_quotes WHERE symbol = $1 ORDER BY timestamp DESC LIMIT 1",
    )
    .bind(&symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?
    .flatten();

    let mut raw_closed_regions = Vec::new();
    let mut current_start = None::<String>;
    for idx in 0..buckets.len() {
        if !buckets[idx].is_market_open {
            if current_start.is_none() {
                current_start = Some(buckets[idx].timestamp.clone());
            }
        } else if let Some(start_ts) = current_start.take() {
            let end_ts = buckets[idx.saturating_sub(1)].timestamp.clone();
            raw_closed_regions.push(ClosedRegion {
                start: start_ts,
                end: end_ts,
            });
        }
    }
    if let (Some(start_ts), Some(last)) = (current_start, buckets.last()) {
        raw_closed_regions.push(ClosedRegion {
            start: start_ts,
            end: last.timestamp.clone(),
        });
    }

    if latest_market_session.as_deref() == Some("regular") && !raw_closed_regions.is_empty() {
        if let (Some(last_region), Some(last_bucket)) = (raw_closed_regions.last(), buckets.last()) {
            if last_region.end == last_bucket.timestamp {
                raw_closed_regions.pop();
            }
        }
    }

    let mut closed_regions = Vec::new();
    for r in raw_closed_regions {
        if let (Ok(t_start), Ok(t_end)) = (
            DateTime::parse_from_rfc3339(&r.start),
            DateTime::parse_from_rfc3339(&r.end),
        ) {
            if (t_end - t_start) >= chrono::Duration::hours(8) {
                closed_regions.push(r);
            }
        }
    }

    // 8. Support & Resistance
    let prices: Vec<f64> = target_market_data.iter().map(|q| q.price).collect();
    let mut support_price = 0.0;
    let mut resistance_price = 0.0;
    let mut support_pct = 0.0;
    let mut resistance_pct = 0.0;
    let latest_price = prices.last().copied();

    if !prices.is_empty() {
        let mut sorted_prices = prices.clone();
        sorted_prices.sort_by(f64::total_cmp);
        support_price = percentile(&sorted_prices, 0.05);
        resistance_price = percentile(&sorted_prices, 0.95);
        if let Some(lp) = latest_price.filter(|&p| p != 0.0) {
            support_pct = (support_price - lp) / lp * 100.0;
            resistance_pct = (resistance_price - lp) / lp * 100.0;
        }
    }

    let rolling_window = 24;
    for i in 0..buckets.len() {
        let start = (i + 1).saturating_sub(rolling_window);
        let mut window_prices = Vec::new();
        for b in &buckets[start..=i] {
            if let Some(rp) = b.raw_price {
                window_prices.push(rp);
            }
        }
        if !window_prices.is_empty() {
            window_prices.sort_by(f64::total_cmp);
            let local_support = percentile(&window_prices, 0.05);
            let local_resistance = percentile(&window_prices, 0.95);
            buckets[i].support_price = Some(local_support);
            buckets[i].resistance_price = Some(local_resistance);
            if let Some(lp) = latest_price.filter(|&p| p != 0.0) {
                buckets[i].support_pct = Some((local_support - lp) / lp * 100.0);
                buckets[i].resistance_pct = Some((local_resistance - lp) / lp * 100.0);
                if let Some(bucket_raw) = buckets[i].raw_price {
                    buckets[i].price_pct = Some((bucket_raw - lp) / lp * 100.0);
                }
            } else {
                buckets[i].support_pct = Some(0.0);
                buckets[i].resistance_pct = Some(0.0);
            }
        } else {
            buckets[i].support_price = Some(support_price);
            buckets[i].support_pct = Some(support_pct);
            buckets[i].resistance_price = Some(resistance_price);
            buckets[i].resistance_pct = Some(resistance_pct);
        }
    }

    // 9. Opportunity inputs and signals
    let pe_relative = sqlx::query_scalar::<_, Option<f64>>(
        "SELECT pe_relative_sector FROM stock_metrics WHERE symbol = $1 ORDER BY updated_at DESC LIMIT 1",
    )
    .bind(&symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?
    .flatten();

    let vix_price_opt = sqlx::query_scalar::<_, Option<f64>>(
        "SELECT price FROM stock_quotes WHERE symbol = $1 ORDER BY timestamp DESC LIMIT 1",
    )
    .bind(&state.vix_symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?
    .flatten();
    let vix_price = vix_price_opt.unwrap_or(15.0);

    for i in 0..buckets.len() {
        let bucket_price = buckets[i].raw_price;
        let mut prev_prices = Vec::new();
        let mut prev_sentiments = Vec::new();
        let start = i.saturating_sub(2);
        for k in start..=i {
            if let Some(rp) = buckets[k].raw_price {
                prev_prices.push(rp);
            }
            prev_sentiments.push(buckets[k].sentiment_index);
        }

        let opp = compute_opportunity(
            bucket_price,
            support_price,
            resistance_price,
            buckets[i].sentiment_index,
            buckets[i].sentiment_sma,
            Some(vix_price),
            pe_relative,
            &prev_prices,
            &prev_sentiments,
        );
        buckets[i].buy_score = Some(opp.score);
        buckets[i].buy_signal = Some(opp.score >= 50.0);
        buckets[i].signal_quality = Some("pending".to_string());
    }

    // Forward returns for signals
    for i in 0..buckets.len() {
        if buckets[i].buy_signal == Some(true) {
            if let Some(curr_price) = buckets[i].raw_price {
                let mut forward_prices = Vec::new();
                let end = (i + 1 + 24).min(buckets.len());
                for k in (i + 1)..end {
                    if let Some(rp) = buckets[k].raw_price {
                        forward_prices.push(rp);
                        if forward_prices.len() >= 2 {
                            break;
                        }
                    }
                }
                if !forward_prices.is_empty() {
                    let max_forward = forward_prices.iter().copied().fold(f64::NEG_INFINITY, f64::max);
                    if max_forward > curr_price * 1.002 {
                        buckets[i].signal_quality = Some("good".to_string());
                    } else {
                        buckets[i].signal_quality = Some("bad".to_string());
                    }
                }
            }
        }
    }

    // 10. Compute final current opportunity
    let latest_item = buckets.last();
    let mut latest_prices = Vec::new();
    let mut latest_sentiments = Vec::new();
    if buckets.len() >= 3 {
        for b in &buckets[buckets.len() - 3..] {
            if let Some(rp) = b.raw_price {
                latest_prices.push(rp);
            }
            latest_sentiments.push(b.sentiment_index);
        }
    }
    let current_opp = latest_item.map(|item| {
        let opp = compute_opportunity(
            item.raw_price,
            support_price,
            resistance_price,
            item.sentiment_index,
            item.sentiment_sma,
            Some(vix_price),
            pe_relative,
            &latest_prices,
            &latest_sentiments,
        );
        OpportunityResponse {
            score: opp.score,
            classification: opp.classification.to_string(),
            color: opp.color.to_string(),
            strategy: opp.strategy.to_string(),
            description: opp.description.to_string(),
            checklist: opp.checklist,
        }
    });

    // 11. Coverage
    let coverage_start = sqlx::query_scalar::<_, Option<DateTime<Utc>>>(
        r#"
        SELECT LEAST(
            (SELECT MIN(timestamp) FROM posts
             WHERE symbol = $1
               AND ($2::TEXT IS NULL OR platform = $2)
               AND ($3::TEXT IS NULL OR topic_label = $3)),
            (SELECT MIN(bucket_hour) FROM hourly_sentiment_facts
             WHERE symbol = $1
               AND ($2::TEXT IS NULL OR platform = $2)
               AND ($3::TEXT IS NULL OR topic_label = $3)),
            CASE WHEN $4::BOOLEAN = FALSE THEN
                (SELECT MIN(bucket_hour) FROM hourly_sentiment_agg WHERE symbol = $1)
            ELSE NULL END
        ) AS coverage_start
        "#,
    )
    .bind(&symbol)
    .bind(platform.as_deref())
    .bind(topic.as_deref())
    .bind(is_filtered)
    .fetch_one(&state.pool)
    .await
    .map_err(internal_error)?;

    let coverage_complete = coverage_start.is_some_and(|start| start <= cutoff);
    let coverage_start_text = coverage_start.map(|value| value.to_rfc3339().replace("+00:00", "Z"));
    let coverage_notice = (!coverage_complete).then(|| {
        let boundary = coverage_start_text
            .as_deref()
            .unwrap_or("the first canonical observation");
        format!("Requested history begins before {boundary}; earlier buckets are reported as unavailable zeros.")
    });

    Ok(Json(CorrelationResponse {
        data: buckets,
        closed_regions,
        support_price,
        support_pct,
        resistance_price,
        resistance_pct,
        max_r,
        best_lag,
        lag_sweeps,
        correlation_text,
        correlation_strength: correlation_strength.to_string(),
        opportunity: current_opp,
        coverage_start: coverage_start_text,
        coverage_end: now.to_rfc3339().replace("+00:00", "Z"),
        coverage_complete,
        coverage_mode: if is_filtered {
            "dimension-preserving".to_string()
        } else {
            "legacy-unfiltered+canonical".to_string()
        },
        coverage_notice,
    }))
}

async fn postgres_listener(database_dsn: String, post_events: broadcast::Sender<String>) {
    loop {
        match PgListener::connect(&database_dsn).await {
            Ok(mut listener) => {
                if let Err(error) = listener.listen("new_posts").await {
                    warn!(%error, "failed to subscribe to PostgreSQL notifications");
                } else {
                    info!("listening for new_posts PostgreSQL notifications");
                    loop {
                        match listener.recv().await {
                            Ok(notification) => {
                                let _ = post_events.send(notification.payload().to_string());
                            }
                            Err(error) => {
                                warn!(%error, "PostgreSQL notification listener disconnected");
                                break;
                            }
                        }
                    }
                }
            }
            Err(error) => warn!(%error, "failed to connect PostgreSQL notification listener"),
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn ws_handler(State(state): State<AppState>, ws: WebSocketUpgrade) -> impl IntoResponse {
    let receiver = state.post_events.subscribe();
    ws.on_upgrade(move |socket| handle_socket(socket, receiver))
}

async fn handle_socket(mut socket: WebSocket, mut receiver: broadcast::Receiver<String>) {
    info!("WebSocket client connected");
    loop {
        tokio::select! {
            incoming = socket.recv() => {
                match incoming {
                    Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                    Some(Ok(_)) => {}
                }
            }
            event = receiver.recv() => {
                match event {
                    Ok(payload) => {
                        if socket.send(Message::Text(payload)).await.is_err() {
                            break;
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(skipped)) => {
                        warn!(skipped, "WebSocket client lagged behind post stream");
                    }
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    }
    info!("WebSocket client disconnected");
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();
    eprintln!("Starting Rust Axum api-service...");
    info!("Starting Rust Axum api-service...");

    let config = Config::from_env();
    let pool = PgPoolOptions::new()
        .max_connections(20)
        .connect(&config.database_dsn)
        .await?;
    let (post_events, _) = broadcast::channel(256);
    tokio::spawn(postgres_listener(
        config.database_dsn.clone(),
        post_events.clone(),
    ));
    let state = AppState {
        pool,
        post_events,
        vix_symbol: config.vix_symbol,
        admin_api_key: std::env::var("ADMIN_API_KEY").unwrap_or_default(),
        global_context_enabled: config.global_context_enabled,
    };

    let mut app = Router::new()
        .route("/health", get(health_handler))
        .route("/api/health", get(health_handler))
        .route("/metrics", get(prometheus_handler))
        .route("/api/v1/sentiment", get(dashboard_handler))
        .route("/api/v1/tracked-symbols", get(tracked_symbols_handler))
        .route("/api/stats/dashboard", get(dashboard_handler))
        .route("/api/stats/sentiment", get(sentiment_stats_handler))
        .route("/api/stats/topics", get(topic_stats_handler))
        .route("/api/stats/correlation", get(correlation_handler))
        .route("/api/stats/leaderboard", get(leaderboard_handler))
        .route("/api/stats/sources", get(sources_handler))
        .route("/api/stats/market", get(market_handler))
        .route("/api/stats/market/latest", get(latest_market_handler))
        .route("/api/stats/market/delta", get(market_delta_handler))
        .route("/api/stats/metrics", get(stock_metrics_handler))
        .route("/api/posts", get(posts_handler))
        .route("/api/stats/posts", get(posts_handler))
        .route("/api/stats/stream", get(ws_handler))
        .route("/ws", get(ws_handler))
        .merge(admin::router())
        .merge(docs::router())
        .merge(global_context::router())
        .with_state(state);
    if let Some(cors) = cors_layer_from_env() {
        app = app.layer(cors);
    }

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8000);

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    eprintln!("Listening on http://{}", addr);
    info!("Listening on http://{}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        compute_opportunity, compute_sentiment_macd, pearson, percentile, source_status,
        validated_hours,
    };

    #[test]
    fn validates_query_window_bounds() {
        assert_eq!(validated_hours(None).expect("default window"), 24);
        assert!(validated_hours(Some(0)).is_err());
        assert!(validated_hours(Some(8_761)).is_err());
    }

    #[test]
    fn computes_pearson_from_observations() {
        let pairs = [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0)];
        assert_eq!(pearson(&pairs), Some(1.0));
        assert_eq!(pearson(&pairs[..2]), None);
        assert_eq!(pearson(&[(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)]), None);
    }

    #[test]
    fn computes_nearest_rank_percentile() {
        let values = [10.0, 20.0, 30.0, 40.0, 50.0];
        assert_eq!(percentile(&values, 0.05), 10.0);
        assert_eq!(percentile(&values, 0.95), 40.0);
        assert_eq!(percentile(&[], 0.5), 0.0);
    }

    #[test]
    fn reports_source_health_from_actual_cadence() {
        assert_eq!(source_status(0, 0, None), "silent");
        assert_eq!(source_status(2, 12, Some(60.0)), "active");
        assert_eq!(source_status(0, 12, Some(60.0)), "quiet");
        assert_eq!(source_status(0, 100, Some(20_000.0)), "stalled");
    }

    #[test]
    fn computes_opportunity_scoring_and_classification() {
        let opp = compute_opportunity(
            Some(101.0),
            100.0,
            120.0,
            0.5,
            0.2,
            Some(14.0),
            Some(-0.5),
            &[105.0, 103.0, 101.0],
            &[0.1, 0.3, 0.5],
        );
        assert_eq!(opp.score, 100.0);
        assert_eq!(opp.classification, "STRONG BUY");
        assert_eq!(opp.color, "emerald");
        assert_eq!(opp.strategy, "Long Call Option (Buy Call)");
        assert_eq!(opp.checklist.len(), 4);
    }

    #[test]
    fn computes_sentiment_macd_carry_forward() {
        let indices = vec![Some(0.1), None, Some(0.3), Some(0.4), Some(0.5), Some(0.6)];
        let macd = compute_sentiment_macd(&indices, 3, 5, 2);
        assert_eq!(macd.len(), 6);
        assert_eq!(macd[0].macd, None);
        assert_eq!(macd[3].macd, None);
        assert!(macd[4].macd.is_some());
    }
}
