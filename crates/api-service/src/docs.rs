use axum::{
    http::header,
    response::{Html, IntoResponse},
    routing::get,
    Json, Router,
};
use serde_json::{json, Value};

use crate::AppState;

async fn openapi() -> Json<Value> {
    let get = |summary: &str| {
        json!({
            "get": {
                "summary": summary,
                "responses": {"200": {"description": "Successful Response"}}
            }
        })
    };
    Json(json!({
        "openapi": "3.1.0",
        "info": {"title": "Social Sentiment API", "version": "0.1.0"},
        "paths": {
            "/metrics": get("Metrics"),
            "/api/admin/symbols": {
                "get": {"summary": "Get Admin Symbols", "responses": {"200": {"description": "Successful Response"}}},
                "post": {"summary": "Create Admin Symbol", "responses": {"200": {"description": "Successful Response"}}}
            },
            "/api/admin/symbols/{symbol}": {
                "put": {"summary": "Update Admin Symbol", "responses": {"200": {"description": "Successful Response"}}},
                "delete": {"summary": "Delete Admin Symbol", "responses": {"200": {"description": "Successful Response"}}}
            },
            "/api/admin/global-context/{symbol}/exposures": {
                "put": {"summary": "Replace Global Context Exposures", "responses": {"200": {"description": "Successful Response"}}}
            },
            "/api/admin/global-context/{symbol}/event-rules": {
                "put": {"summary": "Replace Global Context Event Rules", "responses": {"200": {"description": "Successful Response"}}}
            },
            "/api/posts": get("Get Posts"),
            "/api/stats/sentiment": get("Get Sentiment Stats"),
            "/api/stats/topics": get("Get Topic Stats"),
            "/api/stats/sources": get("Get Source Health"),
            "/api/stats/leaderboard": get("Get Leaderboard"),
            "/api/stats/market": get("Get Market Data"),
            "/api/stats/market/latest": get("Get Latest Market Quote"),
            "/api/stats/market/delta": get("Get Market Delta"),
            "/api/stats/metrics": get("Get Stock Metrics"),
            "/api/stats/global-context": get("Get Global Context"),
            "/api/stats/dashboard": get("Get Dashboard"),
            "/api/stats/correlation": get("Get Correlation"),
            "/api/health": get("Health")
        }
    }))
}

async fn docs() -> impl IntoResponse {
    (
        [(header::CACHE_CONTROL, "no-store")],
        Html(
            r#"<!doctype html>
<html><head><title>Social Sentiment API - Swagger UI</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui'});</script></body></html>"#,
        ),
    )
}

pub(crate) fn router() -> Router<AppState> {
    Router::new()
        .route("/openapi.json", get(openapi))
        .route("/docs", get(docs))
}
