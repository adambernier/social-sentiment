use std::collections::HashSet;

use axum::{
    extract::{Path, State},
    http::HeaderMap,
    routing::{get, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sqlx::{types::Json as SqlJson, Row};

use crate::{api_error::ApiError, AppState};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct TrackedSymbol {
    symbol: String,
    #[serde(default)]
    keywords: Vec<String>,
    future: Option<String>,
    sector: Option<String>,
    #[serde(default)]
    require_uppercase: bool,
    #[serde(default)]
    require_cashtag: bool,
    #[serde(default)]
    block_phrases: Vec<String>,
    #[serde(default = "default_true")]
    is_active: bool,
}

#[derive(Debug, Deserialize)]
pub(crate) struct ExposureInput {
    instrument_key: String,
    reason: String,
    #[serde(default)]
    display_order: i32,
}

#[derive(Debug, Deserialize)]
pub(crate) struct ExposureReplacement {
    #[serde(default)]
    exposures: Vec<ExposureInput>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct EventRuleInput {
    name: String,
    #[serde(default)]
    countries: Vec<String>,
    #[serde(default)]
    themes: Vec<String>,
    #[serde(default)]
    query_terms: Vec<String>,
    #[serde(default = "default_true")]
    is_active: bool,
}

#[derive(Debug, Deserialize)]
pub(crate) struct EventRuleReplacement {
    #[serde(default)]
    rules: Vec<EventRuleInput>,
}

fn default_true() -> bool {
    true
}

fn authorize(headers: &HeaderMap, state: &AppState) -> Result<(), ApiError> {
    if state.admin_api_key.is_empty() {
        return Err(ApiError::forbidden(
            "Admin API Key is not configured on the server",
        ));
    }
    let supplied = headers
        .get("x-api-key")
        .and_then(|value| value.to_str().ok());
    if supplied != Some(state.admin_api_key.as_str()) {
        return Err(ApiError::forbidden("Invalid API Key"));
    }
    Ok(())
}

fn require_global_context(state: &AppState) -> Result<(), ApiError> {
    if state.global_context_enabled {
        Ok(())
    } else {
        Err(ApiError::not_found("Global context is disabled"))
    }
}

fn validate_exposures(replacement: &ExposureReplacement) -> Result<(), ApiError> {
    let mut keys = HashSet::new();
    for exposure in &replacement.exposures {
        if exposure.instrument_key.is_empty() || exposure.instrument_key.len() > 120 {
            return Err(ApiError::unprocessable(
                "instrument_key must contain between 1 and 120 characters",
            ));
        }
        if exposure.reason.is_empty() || exposure.reason.len() > 1_000 {
            return Err(ApiError::unprocessable(
                "reason must contain between 1 and 1000 characters",
            ));
        }
        if exposure.display_order < 0 {
            return Err(ApiError::unprocessable(
                "display_order must be non-negative",
            ));
        }
        if !keys.insert(&exposure.instrument_key) {
            return Err(ApiError::unprocessable(
                "instrument_key values must be unique",
            ));
        }
    }
    Ok(())
}

fn validate_match_values(values: &[String]) -> Result<(), ApiError> {
    if values.len() > 30 {
        return Err(ApiError::unprocessable(
            "rule match lists may contain at most 30 values",
        ));
    }
    let mut unique = HashSet::new();
    for value in values {
        if value.trim().is_empty() {
            return Err(ApiError::unprocessable("rule match values cannot be blank"));
        }
        if !unique.insert(value) {
            return Err(ApiError::unprocessable("rule match values must be unique"));
        }
    }
    Ok(())
}

fn validate_rules(replacement: &EventRuleReplacement) -> Result<(), ApiError> {
    let mut names = HashSet::new();
    for rule in &replacement.rules {
        if rule.name.is_empty() || rule.name.len() > 200 {
            return Err(ApiError::unprocessable(
                "name must contain between 1 and 200 characters",
            ));
        }
        if !names.insert(&rule.name) {
            return Err(ApiError::unprocessable("rule names must be unique"));
        }
        if rule.countries.is_empty() && rule.themes.is_empty() && rule.query_terms.is_empty() {
            return Err(ApiError::unprocessable(
                "each rule needs at least one country, theme, or query term",
            ));
        }
        validate_match_values(&rule.countries)?;
        validate_match_values(&rule.themes)?;
        validate_match_values(&rule.query_terms)?;
    }
    Ok(())
}

async fn get_symbols(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Vec<TrackedSymbol>>, ApiError> {
    authorize(&headers, &state)?;
    let rows = sqlx::query(
        r#"
        SELECT symbol, keywords, future, sector, require_uppercase,
               require_cashtag, block_phrases, is_active
        FROM tracked_symbols
        ORDER BY symbol
        "#,
    )
    .fetch_all(&state.pool)
    .await
    .map_err(ApiError::database)?;

    Ok(Json(
        rows.into_iter()
            .map(|row| TrackedSymbol {
                symbol: row.get("symbol"),
                keywords: row.get::<SqlJson<Vec<String>>, _>("keywords").0,
                future: row.get("future"),
                sector: row.get("sector"),
                require_uppercase: row.get("require_uppercase"),
                require_cashtag: row.get("require_cashtag"),
                block_phrases: row.get::<SqlJson<Vec<String>>, _>("block_phrases").0,
                is_active: row.get("is_active"),
            })
            .collect(),
    ))
}

async fn create_symbol(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(symbol): Json<TrackedSymbol>,
) -> Result<Json<serde_json::Value>, ApiError> {
    authorize(&headers, &state)?;
    let result = sqlx::query(
        r#"
        INSERT INTO tracked_symbols (
            symbol, keywords, future, sector, require_uppercase,
            require_cashtag, block_phrases, is_active
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        "#,
    )
    .bind(&symbol.symbol)
    .bind(SqlJson(&symbol.keywords))
    .bind(&symbol.future)
    .bind(&symbol.sector)
    .bind(symbol.require_uppercase)
    .bind(symbol.require_cashtag)
    .bind(SqlJson(&symbol.block_phrases))
    .bind(symbol.is_active)
    .execute(&state.pool)
    .await;
    match result {
        Ok(_) => Ok(Json(json!({"status": "success"}))),
        Err(sqlx::Error::Database(error)) if error.code().as_deref() == Some("23505") => {
            Err(ApiError::bad_request("Symbol already exists"))
        }
        Err(error) => Err(ApiError::database(error)),
    }
}

async fn update_symbol(
    State(state): State<AppState>,
    Path(symbol_path): Path<String>,
    headers: HeaderMap,
    Json(symbol): Json<TrackedSymbol>,
) -> Result<Json<serde_json::Value>, ApiError> {
    authorize(&headers, &state)?;
    let result = sqlx::query(
        r#"
        UPDATE tracked_symbols
        SET keywords = $1, future = $2, sector = $3,
            require_uppercase = $4, require_cashtag = $5,
            block_phrases = $6, is_active = $7, updated_at = NOW()
        WHERE symbol = $8
        "#,
    )
    .bind(SqlJson(&symbol.keywords))
    .bind(&symbol.future)
    .bind(&symbol.sector)
    .bind(symbol.require_uppercase)
    .bind(symbol.require_cashtag)
    .bind(SqlJson(&symbol.block_phrases))
    .bind(symbol.is_active)
    .bind(symbol_path)
    .execute(&state.pool)
    .await
    .map_err(ApiError::database)?;
    if result.rows_affected() == 0 {
        return Err(ApiError::not_found("Symbol not found"));
    }
    Ok(Json(json!({"status": "success"})))
}

async fn delete_symbol(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, ApiError> {
    authorize(&headers, &state)?;
    let result = sqlx::query(
        "UPDATE tracked_symbols SET is_active = false, updated_at = NOW() WHERE symbol = $1",
    )
    .bind(symbol)
    .execute(&state.pool)
    .await
    .map_err(ApiError::database)?;
    if result.rows_affected() == 0 {
        return Err(ApiError::not_found("Symbol not found"));
    }
    Ok(Json(json!({"status": "success"})))
}

async fn replace_exposures(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    headers: HeaderMap,
    Json(replacement): Json<ExposureReplacement>,
) -> Result<Json<serde_json::Value>, ApiError> {
    authorize(&headers, &state)?;
    require_global_context(&state)?;
    validate_exposures(&replacement)?;
    let symbol = symbol.trim().to_uppercase();
    let exists = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS (SELECT 1 FROM tracked_symbols WHERE symbol = $1)",
    )
    .bind(&symbol)
    .fetch_one(&state.pool)
    .await
    .map_err(ApiError::database)?;
    if !exists {
        return Err(ApiError::not_found("Symbol not found"));
    }

    let mut transaction = state.pool.begin().await.map_err(ApiError::database)?;
    sqlx::query("DELETE FROM stock_factor_exposures WHERE symbol = $1")
        .bind(&symbol)
        .execute(&mut *transaction)
        .await
        .map_err(ApiError::database)?;
    for exposure in &replacement.exposures {
        let result = sqlx::query(
            r#"
            INSERT INTO stock_factor_exposures
                (symbol, instrument_key, reason, display_order)
            VALUES ($1, $2, $3, $4)
            "#,
        )
        .bind(&symbol)
        .bind(&exposure.instrument_key)
        .bind(&exposure.reason)
        .bind(exposure.display_order)
        .execute(&mut *transaction)
        .await;
        if let Err(sqlx::Error::Database(error)) = &result {
            if error.code().as_deref() == Some("23503") {
                return Err(ApiError::bad_request(
                    "An exposure references an unknown instrument_key",
                ));
            }
        }
        result.map_err(ApiError::database)?;
    }
    transaction.commit().await.map_err(ApiError::database)?;
    Ok(Json(
        json!({"status": "success", "replaced": replacement.exposures.len()}),
    ))
}

async fn replace_event_rules(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    headers: HeaderMap,
    Json(replacement): Json<EventRuleReplacement>,
) -> Result<Json<serde_json::Value>, ApiError> {
    authorize(&headers, &state)?;
    require_global_context(&state)?;
    validate_rules(&replacement)?;
    let symbol = symbol.trim().to_uppercase();
    let exists = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS (SELECT 1 FROM tracked_symbols WHERE symbol = $1)",
    )
    .bind(&symbol)
    .fetch_one(&state.pool)
    .await
    .map_err(ApiError::database)?;
    if !exists {
        return Err(ApiError::not_found("Symbol not found"));
    }

    let mut transaction = state.pool.begin().await.map_err(ApiError::database)?;
    sqlx::query("DELETE FROM global_event_rules WHERE symbol = $1")
        .bind(&symbol)
        .execute(&mut *transaction)
        .await
        .map_err(ApiError::database)?;
    for rule in &replacement.rules {
        sqlx::query(
            r#"
            INSERT INTO global_event_rules
                (symbol, name, countries, themes, query_terms, is_active)
            VALUES ($1, $2, $3, $4, $5, $6)
            "#,
        )
        .bind(&symbol)
        .bind(&rule.name)
        .bind(SqlJson(&rule.countries))
        .bind(SqlJson(&rule.themes))
        .bind(SqlJson(&rule.query_terms))
        .bind(rule.is_active)
        .execute(&mut *transaction)
        .await
        .map_err(ApiError::database)?;
    }
    transaction.commit().await.map_err(ApiError::database)?;
    Ok(Json(
        json!({"status": "success", "replaced": replacement.rules.len()}),
    ))
}

pub(crate) fn router() -> Router<AppState> {
    Router::new()
        .route("/api/admin/symbols", get(get_symbols).post(create_symbol))
        .route(
            "/api/admin/symbols/:symbol",
            put(update_symbol).delete(delete_symbol),
        )
        .route(
            "/api/admin/global-context/:symbol/exposures",
            put(replace_exposures),
        )
        .route(
            "/api/admin/global-context/:symbol/event-rules",
            put(replace_event_rules),
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replacement_validation_rejects_duplicate_exposures() {
        let replacement = ExposureReplacement {
            exposures: vec![
                ExposureInput {
                    instrument_key: "index:test".to_string(),
                    reason: "first".to_string(),
                    display_order: 0,
                },
                ExposureInput {
                    instrument_key: "index:test".to_string(),
                    reason: "second".to_string(),
                    display_order: 1,
                },
            ],
        };
        assert!(validate_exposures(&replacement).is_err());
    }

    #[test]
    fn event_rule_requires_at_least_one_match_dimension() {
        let replacement = EventRuleReplacement {
            rules: vec![EventRuleInput {
                name: "empty".to_string(),
                countries: Vec::new(),
                themes: Vec::new(),
                query_terms: Vec::new(),
                is_active: true,
            }],
        };
        assert!(validate_rules(&replacement).is_err());
    }
}
