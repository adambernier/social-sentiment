use std::collections::HashMap;

use axum::{extract::Query, extract::State, routing::get, Json, Router};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use social_sentiment_core::analytics::{
    calculate_relationship, next_close_move, DailyClose, RelationshipStatistic,
};
use sqlx::Row;

use crate::{api_error::ApiError, AppState};

const CURRENCY_ORIENTATION: &str =
    "Asian FX is local-currency units per USD; positive means local-currency weakness.";
const DISCLAIMER: &str = "Measured association, not causation or a forecast.";

#[derive(Debug, Deserialize)]
struct GlobalContextQuery {
    symbol: String,
    horizon_sessions: Option<u16>,
}

#[derive(Debug, Serialize)]
struct GlobalFactorResponse {
    instrument_key: String,
    display_name: String,
    asset_class: String,
    currency: String,
    exchange: Option<String>,
    timezone: String,
    quote_convention: Option<String>,
    exposure_reason: String,
    current_price: Option<f64>,
    current_move_pct: Option<f64>,
    current_as_of: Option<DateTime<Utc>>,
    fetched_at: Option<DateTime<Utc>>,
    provider: Option<String>,
    relationship: RelationshipStatistic,
}

#[derive(Debug, Serialize)]
struct GlobalEventResponse {
    id: i64,
    title: String,
    summary: Option<String>,
    canonical_url: Option<String>,
    source_name: Option<String>,
    occurred_at: DateTime<Utc>,
    provider: String,
    rule_names: Vec<String>,
    match_reasons: Vec<Value>,
    next_close_move_pct: Option<f64>,
    reaction_label: &'static str,
}

#[derive(Debug, Serialize)]
struct GlobalFreshnessResponse {
    latest_factor_at: Option<DateTime<Utc>>,
    latest_daily_at: Option<DateTime<Utc>>,
    latest_event_at: Option<DateTime<Utc>>,
    status: &'static str,
}

#[derive(Debug, Serialize)]
struct GlobalContextResponse {
    symbol: String,
    configured: bool,
    horizon_sessions: u16,
    as_of: DateTime<Utc>,
    currency_orientation: &'static str,
    disclaimer: &'static str,
    factors: Vec<GlobalFactorResponse>,
    events: Vec<GlobalEventResponse>,
    freshness: GlobalFreshnessResponse,
}

struct FactorSnapshot {
    instrument_key: String,
    exposure_reason: String,
    display_name: String,
    asset_class: String,
    currency: String,
    exchange: Option<String>,
    timezone: String,
    quote_convention: Option<String>,
    current_price: Option<f64>,
    current_as_of: Option<DateTime<Utc>>,
    fetched_at: Option<DateTime<Utc>>,
    provider: Option<String>,
    reference_price: Option<f64>,
}

fn empty_response(
    symbol: String,
    horizon_sessions: u16,
    as_of: DateTime<Utc>,
) -> GlobalContextResponse {
    GlobalContextResponse {
        symbol,
        configured: false,
        horizon_sessions,
        as_of,
        currency_orientation: CURRENCY_ORIENTATION,
        disclaimer: DISCLAIMER,
        factors: Vec::new(),
        events: Vec::new(),
        freshness: GlobalFreshnessResponse {
            latest_factor_at: None,
            latest_daily_at: None,
            latest_event_at: None,
            status: "empty",
        },
    }
}

fn current_move(current: Option<f64>, reference: Option<f64>) -> Option<f64> {
    current.zip(reference).and_then(|(current, reference)| {
        (reference > 0.0).then_some((current / reference - 1.0) * 100.0)
    })
}

fn json_strings(value: Value) -> Vec<String> {
    serde_json::from_value(value).unwrap_or_default()
}

fn json_values(value: Value) -> Vec<Value> {
    value.as_array().cloned().unwrap_or_default()
}

async fn get_global_context(
    State(state): State<AppState>,
    Query(query): Query<GlobalContextQuery>,
) -> Result<Json<GlobalContextResponse>, ApiError> {
    if !state.global_context_enabled {
        return Err(ApiError::not_found("Global context is disabled"));
    }
    let horizon_sessions = query.horizon_sessions.unwrap_or(30);
    if !matches!(horizon_sessions, 30 | 90) {
        return Err(ApiError::unprocessable(
            "horizon_sessions must be either 30 or 90",
        ));
    }
    let symbol = query.symbol.trim().to_uppercase();
    let as_of = Utc::now();

    let factor_rows = sqlx::query(
        r#"
        SELECT exposure.instrument_key, exposure.reason AS exposure_reason,
               instrument.display_name, instrument.asset_class,
               instrument.currency, instrument.exchange, instrument.timezone,
               instrument.quote_convention,
               latest.close_price AS current_price,
               latest.ends_at AS current_as_of, latest.fetched_at,
               latest.provider, reference.close_price AS reference_price
        FROM stock_factor_exposures AS exposure
        JOIN global_instruments AS instrument
          ON instrument.instrument_key = exposure.instrument_key
        LEFT JOIN LATERAL (
            SELECT bar.close_price, bar.ends_at, bar.fetched_at, bar.interval,
                   bar.provider
            FROM global_market_bars AS bar
            WHERE bar.instrument_key = exposure.instrument_key
            ORDER BY bar.ends_at DESC,
                     CASE WHEN bar.interval = '1h' THEN 0 ELSE 1 END
            LIMIT 1
        ) AS latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT bar.close_price
            FROM global_market_bars AS bar
            WHERE bar.instrument_key = exposure.instrument_key
              AND bar.interval = '1d'
              AND bar.ends_at < latest.ends_at
            ORDER BY bar.ends_at DESC
            LIMIT 1
        ) AS reference ON TRUE
        WHERE exposure.symbol = $1
          AND instrument.is_active
          AND instrument.asset_class <> 'us_equity'
        ORDER BY exposure.display_order, instrument.display_name
        "#,
    )
    .bind(&symbol)
    .fetch_all(&state.pool)
    .await
    .map_err(ApiError::database)?;

    if factor_rows.is_empty() {
        return Ok(Json(empty_response(symbol, horizon_sessions, as_of)));
    }
    let snapshots = factor_rows
        .into_iter()
        .map(|row| FactorSnapshot {
            instrument_key: row.get("instrument_key"),
            exposure_reason: row.get("exposure_reason"),
            display_name: row.get("display_name"),
            asset_class: row.get("asset_class"),
            currency: row.get("currency"),
            exchange: row.get("exchange"),
            timezone: row.get("timezone"),
            quote_convention: row.get("quote_convention"),
            current_price: row.get("current_price"),
            current_as_of: row.get("current_as_of"),
            fetched_at: row.get("fetched_at"),
            provider: row.get("provider"),
            reference_price: row.get("reference_price"),
        })
        .collect::<Vec<_>>();

    let target_key = format!("us-stock:{symbol}");
    let mut instrument_keys = snapshots
        .iter()
        .map(|snapshot| snapshot.instrument_key.clone())
        .collect::<Vec<_>>();
    instrument_keys.push(target_key.clone());
    let history_rows = sqlx::query(
        r#"
        SELECT instrument_key, ends_at, close_price, fetched_at
        FROM global_market_bars
        WHERE interval = '1d'
          AND instrument_key = ANY($1)
          AND ends_at >= NOW() - INTERVAL '2 years'
        ORDER BY instrument_key, ends_at
        "#,
    )
    .bind(&instrument_keys)
    .fetch_all(&state.pool)
    .await
    .map_err(ApiError::database)?;

    let mut history: HashMap<String, Vec<DailyClose>> = HashMap::new();
    let mut latest_daily_at = None;
    for row in history_rows {
        let fetched_at: DateTime<Utc> = row.get("fetched_at");
        latest_daily_at = Some(
            latest_daily_at.map_or(fetched_at, |latest: DateTime<Utc>| latest.max(fetched_at)),
        );
        history
            .entry(row.get("instrument_key"))
            .or_default()
            .push(DailyClose {
                ends_at: row.get("ends_at"),
                close_price: row.get("close_price"),
            });
    }
    let target_closes = history.get(&target_key).cloned().unwrap_or_default();
    let mut latest_factor_at = None;
    let factors = snapshots
        .into_iter()
        .map(|snapshot| {
            if let Some(fetched_at) = snapshot.fetched_at {
                latest_factor_at = Some(
                    latest_factor_at
                        .map_or(fetched_at, |latest: DateTime<Utc>| latest.max(fetched_at)),
                );
            }
            let relationship = calculate_relationship(
                history
                    .get(&snapshot.instrument_key)
                    .map(Vec::as_slice)
                    .unwrap_or_default(),
                &target_closes,
                usize::from(horizon_sessions),
            );
            GlobalFactorResponse {
                current_move_pct: current_move(snapshot.current_price, snapshot.reference_price),
                instrument_key: snapshot.instrument_key,
                display_name: snapshot.display_name,
                asset_class: snapshot.asset_class,
                currency: snapshot.currency,
                exchange: snapshot.exchange,
                timezone: snapshot.timezone,
                quote_convention: snapshot.quote_convention,
                exposure_reason: snapshot.exposure_reason,
                current_price: snapshot.current_price,
                current_as_of: snapshot.current_as_of,
                fetched_at: snapshot.fetched_at,
                provider: snapshot.provider,
                relationship,
            }
        })
        .collect();

    let event_rows = sqlx::query(
        r#"
        SELECT signal.id, signal.title, signal.summary, signal.canonical_url,
               signal.source_name, signal.occurred_at, signal.provider,
               signal.ingested_at,
               jsonb_agg(rule.name ORDER BY rule.name) AS rule_names,
               jsonb_agg(link.match_reason ORDER BY rule.name) AS match_reasons
        FROM stock_event_links AS link
        JOIN global_event_signals AS signal ON signal.id = link.event_id
        JOIN global_event_rules AS rule ON rule.id = link.rule_id
        WHERE link.symbol = $1
          AND signal.occurred_at >= NOW() - INTERVAL '30 days'
        GROUP BY signal.id
        ORDER BY signal.occurred_at DESC
        LIMIT 30
        "#,
    )
    .bind(&symbol)
    .fetch_all(&state.pool)
    .await
    .map_err(ApiError::database)?;
    let mut latest_event_at = None;
    let events = event_rows
        .into_iter()
        .map(|row| {
            let occurred_at = row.get("occurred_at");
            let ingested_at: DateTime<Utc> = row.get("ingested_at");
            latest_event_at = Some(
                latest_event_at
                    .map_or(ingested_at, |latest: DateTime<Utc>| latest.max(ingested_at)),
            );
            GlobalEventResponse {
                id: row.get("id"),
                title: row.get("title"),
                summary: row.get("summary"),
                canonical_url: row.get("canonical_url"),
                source_name: row.get("source_name"),
                occurred_at,
                provider: row.get("provider"),
                rule_names: json_strings(row.get("rule_names")),
                match_reasons: json_values(row.get("match_reasons")),
                next_close_move_pct: next_close_move(&target_closes, occurred_at),
                reaction_label: "next-close move",
            }
        })
        .collect();
    let status = match latest_factor_at {
        None => "empty",
        Some(latest) if (as_of - latest).num_seconds() > 72 * 3_600 => "stale",
        Some(_) => "fresh",
    };

    Ok(Json(GlobalContextResponse {
        symbol,
        configured: true,
        horizon_sessions,
        as_of,
        currency_orientation: CURRENCY_ORIENTATION,
        disclaimer: DISCLAIMER,
        factors,
        events,
        freshness: GlobalFreshnessResponse {
            latest_factor_at,
            latest_daily_at,
            latest_event_at,
            status,
        },
    }))
}

pub(crate) fn router() -> Router<AppState> {
    Router::new().route("/api/stats/global-context", get(get_global_context))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn current_move_requires_a_positive_reference() {
        let movement = current_move(Some(105.0), Some(100.0)).expect("movement");
        assert!((movement - 5.0).abs() < 1e-12);
        assert_eq!(current_move(Some(105.0), Some(0.0)), None);
        assert_eq!(current_move(None, Some(100.0)), None);
    }

    #[test]
    fn unconfigured_contract_is_typed_and_empty() {
        let response = empty_response("NVDA".to_string(), 30, Utc::now());
        assert!(!response.configured);
        assert!(response.factors.is_empty());
        assert!(response.events.is_empty());
        assert_eq!(response.freshness.status, "empty");
    }
}
