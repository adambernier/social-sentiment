use anyhow::Result;
use axum::{
    extract::{Query, State, WebSocketUpgrade, ws::WebSocket},
    http::StatusCode,
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use social_sentiment_core::config::Config;
use sqlx::{postgres::PgPoolOptions, PgPool, Row};
use std::net::SocketAddr;
use tower_http::cors::{Any, CorsLayer};
use tracing::{error, info};

#[derive(Clone)]
struct AppState {
    pool: PgPool,
}

#[derive(Debug, Serialize, Deserialize)]
struct HealthResponse {
    status: &'static str,
    timestamp: DateTime<Utc>,
}

#[derive(Debug, Deserialize)]
struct SentimentParams {
    symbol: Option<String>,
    hours: Option<i64>,
}

#[derive(Debug, Serialize)]
struct SentimentPoint {
    symbol: String,
    bucket_hour: DateTime<Utc>,
    positive_count: i64,
    neutral_count: i64,
    negative_count: i64,
    sentiment_index: f64,
}

async fn health_handler() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        timestamp: Utc::now(),
    })
}

async fn sentiment_handler(
    State(state): State<AppState>,
    Query(params): Query<SentimentParams>,
) -> Result<Json<Vec<SentimentPoint>>, (StatusCode, String)> {
    let hours = params.hours.unwrap_or(24);
    let cutoff = Utc::now() - chrono::Duration::hours(hours);

    let query_str = if params.symbol.is_some() {
        r#"
        SELECT symbol, bucket_hour, positive_count, neutral_count, negative_count, sentiment_index
        FROM hourly_sentiment_agg
        WHERE symbol = $1 AND bucket_hour >= $2
        ORDER BY bucket_hour ASC
        "#
    } else {
        r#"
        SELECT symbol, bucket_hour, positive_count, neutral_count, negative_count, sentiment_index
        FROM hourly_sentiment_agg
        WHERE bucket_hour >= $1
        ORDER BY bucket_hour ASC
        "#
    };

    let rows = if let Some(ref sym) = params.symbol {
        sqlx::query(query_str)
            .bind(sym)
            .bind(cutoff)
            .fetch_all(&state.pool)
            .await
    } else {
        sqlx::query(query_str)
            .bind(cutoff)
            .fetch_all(&state.pool)
            .await
    };

    match rows {
        Ok(records) => {
            let points = records
                .into_iter()
                .map(|r| SentimentPoint {
                    symbol: r.get("symbol"),
                    bucket_hour: r.get("bucket_hour"),
                    positive_count: r.get("positive_count"),
                    neutral_count: r.get("neutral_count"),
                    negative_count: r.get("negative_count"),
                    sentiment_index: r.get("sentiment_index"),
                })
                .collect();
            Ok(Json(points))
        }
        Err(e) => {
            error!("Database query error: {}", e);
            Err((StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
        }
    }
}

async fn tracked_symbols_handler(
    State(state): State<AppState>,
) -> Result<Json<Vec<String>>, (StatusCode, String)> {
    let rows = sqlx::query("SELECT symbol FROM tracked_symbols WHERE is_active ORDER BY symbol")
        .fetch_all(&state.pool)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let symbols: Vec<String> = rows.into_iter().map(|r| r.get("symbol")).collect();
    Ok(Json(symbols))
}

async fn ws_handler(ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(handle_socket)
}

async fn handle_socket(mut socket: WebSocket) {
    info!("WebSocket client connected");
    while let Some(msg) = socket.recv().await {
        if msg.is_err() {
            break;
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

    let state = AppState { pool };

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let app = Router::new()
        .route("/health", get(health_handler))
        .route("/api/v1/sentiment", get(sentiment_handler))
        .route("/api/v1/tracked-symbols", get(tracked_symbols_handler))
        .route("/ws", get(ws_handler))
        .layer(cors)
        .with_state(state);

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8000);

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    info!("Listening on http://{}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
