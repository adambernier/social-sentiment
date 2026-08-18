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
    let latest_quote = market_data.last().cloned();

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

    let primary_delta = market_delta(&market_data);
    let primary_future_symbol = sqlx::query_scalar::<_, Option<String>>(
        "SELECT future FROM tracked_symbols WHERE symbol = $1 AND is_active",
    )
    .bind(&symbol)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal_error)?
    .flatten();
    let primary_future_market_data = if let Some(future_symbol) = primary_future_symbol.as_deref() {
        fetch_market_series(&state.pool, future_symbol, cutoff).await?
    } else {
        Vec::new()
    };
    let primary_future_quote = primary_future_market_data.last().cloned();
    let primary_future_delta = market_delta(&primary_future_market_data);
    let vix_market_data = fetch_market_series(&state.pool, &state.vix_symbol, cutoff).await?;
    let vix_quote = vix_market_data.last().cloned();
    let vix_delta = market_delta(&vix_market_data);

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

fn lagged_correlations(buckets: &[CorrelationBucket]) -> (f64, i32, Vec<LagSweepValue>) {
    let mut best = None::<(f64, i32)>;
    let mut sweeps = Vec::new();
    for lag in -6_i32..=6 {
        let mut pairs = Vec::new();
        for price_index in 0..buckets.len() {
            let sentiment_index = price_index as i64 - i64::from(lag);
            if sentiment_index < 0 || sentiment_index >= buckets.len() as i64 {
                continue;
            }
            let price_bucket = &buckets[price_index];
            let sentiment_bucket = &buckets[sentiment_index as usize];
            if !price_bucket.is_market_open
                || sentiment_bucket.positive + sentiment_bucket.neutral + sentiment_bucket.negative
                    == 0
            {
                continue;
            }
            if let Some(change) = price_bucket.price_change {
                pairs.push((sentiment_bucket.sentiment_index, change));
            }
        }
        let correlation = pearson(&pairs).unwrap_or(0.0);
        if pearson(&pairs).is_some()
            && best.is_none_or(|(current, _)| correlation.abs() > current.abs())
        {
            best = Some((correlation, lag));
        }
        sweeps.push(LagSweepValue {
            lag,
            r: correlation,
        });
    }
    let (max_r, best_lag) = best.unwrap_or((0.0, 0));
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
    let cutoff = now - chrono::Duration::hours(hours);

    let agg_rows = sqlx::query(
        r#"
        SELECT date_trunc('hour', timestamp) AS bucket_hour,
               COUNT(*) FILTER (WHERE sentiment = 'positive') AS pos_cnt,
               COUNT(*) FILTER (WHERE sentiment = 'neutral') AS neu_cnt,
               COUNT(*) FILTER (WHERE sentiment = 'negative') AS neg_cnt
        FROM posts
        WHERE symbol = $1 AND timestamp >= $2
          AND ($3::TEXT IS NULL OR platform = $3)
          AND ($4::TEXT IS NULL OR topic_label = $4)
        GROUP BY date_trunc('hour', timestamp)
        ORDER BY bucket_hour
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .bind(platform.as_deref())
    .bind(topic.as_deref())
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let mut agg_map = std::collections::HashMap::new();
    for row in agg_rows {
        agg_map.insert(
            row.get::<DateTime<Utc>, _>("bucket_hour"),
            (
                row.get::<i64, _>("pos_cnt"),
                row.get::<i64, _>("neu_cnt"),
                row.get::<i64, _>("neg_cnt"),
            ),
        );
    }

    let price_rows = sqlx::query(
        r#"
        SELECT date_trunc('hour', timestamp) AS bucket_hour,
               (array_agg(price ORDER BY timestamp DESC))[1] AS close_price,
               bool_or(market_session = 'regular') AS is_open
        FROM stock_quotes
        WHERE symbol = $1 AND timestamp >= $2
        GROUP BY date_trunc('hour', timestamp)
        ORDER BY bucket_hour
        "#,
    )
    .bind(&symbol)
    .bind(cutoff)
    .fetch_all(&state.pool)
    .await
    .map_err(internal_error)?;

    let mut price_map = std::collections::HashMap::new();
    for row in price_rows {
        price_map.insert(
            row.get::<DateTime<Utc>, _>("bucket_hour"),
            (
                row.get::<f64, _>("close_price"),
                row.get::<bool, _>("is_open"),
            ),
        );
    }

    let coverage_start = sqlx::query_scalar::<_, Option<DateTime<Utc>>>(
        r#"
        SELECT MIN(timestamp)
        FROM posts
        WHERE symbol = $1
          AND ($2::TEXT IS NULL OR platform = $2)
          AND ($3::TEXT IS NULL OR topic_label = $3)
        "#,
    )
    .bind(&symbol)
    .bind(platform.as_deref())
    .bind(topic.as_deref())
    .fetch_one(&state.pool)
    .await
    .map_err(internal_error)?;

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
    let mut buckets = Vec::with_capacity(hours as usize + 1);
    let mut previous_price = None::<f64>;
    for offset in (0..=hours).rev() {
        let bucket_time = current_hour - chrono::Duration::hours(offset);
        let (positive, neutral, negative) = agg_map.get(&bucket_time).copied().unwrap_or((0, 0, 0));
        let total = positive + neutral + negative;
        let sentiment_index = if total == 0 {
            0.0
        } else {
            (positive - negative) as f64 / total as f64
        };
        let (raw_price, is_market_open) = price_map
            .get(&bucket_time)
            .copied()
            .map_or((None, false), |(price, open)| (Some(price), open));
        let price_change = raw_price.zip(previous_price).map(|(price, previous)| {
            if previous == 0.0 {
                0.0
            } else {
                (price - previous) / previous * 100.0
            }
        });
        if raw_price.is_some() {
            previous_price = raw_price;
        }
        buckets.push(CorrelationBucket {
            timestamp: bucket_time.to_rfc3339().replace("+00:00", "Z"),
            positive,
            neutral,
            negative,
            price_change,
            price_pct: None,
            future_change: None,
            future_pct: None,
            is_market_open,
            sentiment_index,
            sentiment_sma: sentiment_index,
            raw_price,
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

    let sma_window = match hours {
        1 => 3,
        2..=24 => 5,
        25..=168 => 12,
        169..=720 => 24,
        _ => 52,
    };
    for index in 0..buckets.len() {
        let start = (index + 1).saturating_sub(sma_window);
        let sum = buckets[start..=index]
            .iter()
            .map(|bucket| bucket.sentiment_index)
            .sum::<f64>();
        buckets[index].sentiment_sma = sum / (index + 1 - start) as f64;
    }

    let mut prices = buckets
        .iter()
        .filter_map(|bucket| bucket.raw_price)
        .filter(|price| price.is_finite())
        .collect::<Vec<_>>();
    let latest_price = prices.last().copied();
    prices.sort_by(f64::total_cmp);
    let support_price = percentile(&prices, 0.05);
    let resistance_price = percentile(&prices, 0.95);
    let (support_pct, resistance_pct) =
        latest_price
            .filter(|price| *price != 0.0)
            .map_or((0.0, 0.0), |price| {
                (
                    (support_price - price) / price * 100.0,
                    (resistance_price - price) / price * 100.0,
                )
            });
    for index in 0..buckets.len() {
        let start = (index + 1).saturating_sub(24);
        let mut local_prices = buckets[start..=index]
            .iter()
            .filter_map(|bucket| bucket.raw_price)
            .collect::<Vec<_>>();
        local_prices.sort_by(f64::total_cmp);
        if !local_prices.is_empty() {
            let local_support = percentile(&local_prices, 0.05);
            let local_resistance = percentile(&local_prices, 0.95);
            buckets[index].support_price = Some(local_support);
            buckets[index].resistance_price = Some(local_resistance);
            if let Some(price) = latest_price.filter(|price| *price != 0.0) {
                buckets[index].support_pct = Some((local_support - price) / price * 100.0);
                buckets[index].resistance_pct = Some((local_resistance - price) / price * 100.0);
                buckets[index].price_pct = buckets[index]
                    .raw_price
                    .map(|bucket_price| (bucket_price - price) / price * 100.0);
            }
        }
    }

    let (max_r, best_lag, lag_sweeps) = lagged_correlations(&buckets);
    let correlation_strength = if max_r.abs() > 0.6 {
        "strong"
    } else if max_r.abs() > 0.35 {
        "moderate"
    } else {
        "weak"
    };
    let correlation_text = if max_r.abs() < 0.15 {
        format!("Weak or no correlation (r = {max_r:+.2})")
    } else if best_lag > 0 {
        format!("Sentiment leads price by {best_lag}h (r = {max_r:+.2})")
    } else if best_lag < 0 {
        format!("Price leads sentiment by {}h (r = {max_r:+.2})", -best_lag)
    } else {
        format!("Coincident correlation (r = {max_r:+.2})")
    };

    let mut closed_regions = Vec::new();
    if !price_map.is_empty() {
        let mut start = None::<String>;
        for (index, bucket) in buckets.iter().enumerate() {
            if !bucket.is_market_open {
                start.get_or_insert_with(|| bucket.timestamp.clone());
            } else if let Some(region_start) = start.take() {
                let end = buckets[index.saturating_sub(1)].timestamp.clone();
                closed_regions.push(ClosedRegion {
                    start: region_start,
                    end,
                });
            }
        }
        if let (Some(region_start), Some(last)) = (start, buckets.last()) {
            closed_regions.push(ClosedRegion {
                start: region_start,
                end: last.timestamp.clone(),
            });
        }
    }

    let coverage_complete = coverage_start.is_some_and(|start| start <= cutoff);
    let coverage_start_text = coverage_start.map(|value| value.to_rfc3339().replace("+00:00", "Z"));
    let coverage_notice = (!coverage_complete).then(|| {
        let boundary = coverage_start_text
            .as_deref()
            .unwrap_or("the first available observation");
        format!("Requested history begins before {boundary}; earlier buckets are unavailable.")
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
        opportunity: None,
        coverage_start: coverage_start_text,
        coverage_end: now.to_rfc3339().replace("+00:00", "Z"),
        coverage_complete,
        coverage_mode: if is_filtered {
            "dimension-preserving".to_string()
        } else {
            "live".to_string()
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
    tracing_subscriber::fmt::init();
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
    info!("Listening on http://{}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{pearson, percentile, source_status, validated_hours};

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
}
