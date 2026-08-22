use std::time::Duration;

use anyhow::{bail, Context, Result};
use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, Utc};
use regex::Regex;
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use social_sentiment_core::producer::{AdapterOutcome, ProviderStatus};
use sqlx::{types::Json, PgPool, Postgres, Row, Transaction};
use url::Url;

pub const GDELT_DOC_ENDPOINT: &str = "https://api.gdeltproject.org/api/v2/doc/doc";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventRule {
    pub id: i64,
    pub symbol: String,
    pub name: String,
    pub countries: Vec<String>,
    pub themes: Vec<String>,
    pub query_terms: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NormalizedEventSignal {
    pub provider: String,
    pub provider_event_id: String,
    pub canonical_url: String,
    pub title: String,
    pub summary: Option<String>,
    pub source_name: Option<String>,
    pub occurred_at: DateTime<Utc>,
    pub countries: Vec<String>,
    pub themes: Vec<String>,
}

#[async_trait]
pub trait GlobalEventAdapter: Send + Sync {
    fn provider_name(&self) -> &'static str;

    async fn fetch(
        &self,
        rule: &EventRule,
        since: DateTime<Utc>,
        max_results: usize,
    ) -> Result<AdapterOutcome<NormalizedEventSignal>>;
}

fn query_atom(value: &str) -> Result<String> {
    let unsafe_character = Regex::new(r#"[^\p{L}\p{N}_ .,'&+:/()-]"#)?;
    let cleaned = unsafe_character
        .replace_all(value, " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if cleaned.is_empty() {
        bail!("event query values cannot be empty");
    }
    Ok(format!("\"{cleaned}\""))
}

pub fn build_gdelt_query(rule: &EventRule) -> Result<String> {
    let mut groups = Vec::new();
    if !rule.query_terms.is_empty() {
        groups.push(format!(
            "({})",
            rule.query_terms
                .iter()
                .map(|value| query_atom(value))
                .collect::<Result<Vec<_>>>()?
                .join(" OR ")
        ));
    }
    if !rule.countries.is_empty() {
        groups.push(format!(
            "({})",
            rule.countries
                .iter()
                .map(|value| query_atom(value))
                .collect::<Result<Vec<_>>>()?
                .join(" OR ")
        ));
    }
    if !rule.themes.is_empty() {
        groups.push(format!(
            "({})",
            rule.themes
                .iter()
                .map(|value| query_atom(value).map(|atom| format!("theme:{atom}")))
                .collect::<Result<Vec<_>>>()?
                .join(" OR ")
        ));
    }
    if groups.is_empty() {
        bail!("event rule must contain a country, theme, or term");
    }
    Ok(groups.join(" AND "))
}

/// Canonicalize URL identity without changing meaningful path/query content.
/// Fragments never reach an origin and query ordering must not create a second
/// event ID for the same article.
pub fn normalize_url(raw: &str) -> Result<String> {
    let mut url = Url::parse(raw).context("invalid event URL")?;
    if !matches!(url.scheme(), "http" | "https") {
        bail!("event URL must use HTTP(S)");
    }
    url.set_fragment(None);
    let mut pairs = url
        .query_pairs()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect::<Vec<_>>();
    pairs.sort();
    url.set_query(None);
    if !pairs.is_empty() {
        url.query_pairs_mut().extend_pairs(&pairs);
    }
    Ok(url.to_string())
}

pub fn parse_gdelt_timestamp(value: Option<&str>, fallback: DateTime<Utc>) -> DateTime<Utc> {
    let Some(value) = value else { return fallback };
    for pattern in ["%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"] {
        if let Ok(parsed) = chrono::NaiveDateTime::parse_from_str(value, pattern) {
            return parsed.and_utc();
        }
    }
    DateTime::parse_from_rfc3339(value)
        .map(|parsed| parsed.with_timezone(&Utc))
        .unwrap_or(fallback)
}

pub fn parse_gdelt_payload(
    payload: &[u8],
    rule: &EventRule,
    now: DateTime<Utc>,
) -> AdapterOutcome<NormalizedEventSignal> {
    let value: Value = match serde_json::from_slice(payload) {
        Ok(Value::Object(root)) => Value::Object(root),
        Ok(_) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
            outcome.detail = Some("invalid GDELT response root".into());
            return outcome;
        }
        Err(error) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
            outcome.detail = Some(format!("invalid GDELT JSON: {error}"));
            return outcome;
        }
    };
    let Some(articles) = value.get("articles").and_then(Value::as_array) else {
        let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
        outcome.detail = Some("invalid GDELT articles array".into());
        return outcome;
    };
    let mut seen = std::collections::HashSet::new();
    let mut items = Vec::new();
    for article in articles {
        let raw_url = article
            .get("url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let title = article
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if raw_url.is_empty() || title.is_empty() {
            continue;
        }
        let Ok(url) = normalize_url(raw_url) else {
            continue;
        };
        if !seen.insert(url.clone()) {
            continue;
        }
        let provider_event_id = format!("{:x}", Sha256::digest(url.as_bytes()));
        items.push(NormalizedEventSignal {
            provider: "gdelt".into(),
            provider_event_id,
            canonical_url: url,
            title: title.chars().take(1_000).collect(),
            summary: None,
            source_name: article
                .get("domain")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|domain| !domain.is_empty())
                .map(|domain| domain.chars().take(255).collect()),
            occurred_at: parse_gdelt_timestamp(
                article.get("seendate").and_then(Value::as_str),
                now,
            ),
            countries: rule.countries.clone(),
            themes: rule.themes.clone(),
        });
    }
    AdapterOutcome::success(items)
}

pub struct GdeltEventAdapter {
    client: Client,
}

impl GdeltEventAdapter {
    pub fn new(client: Client) -> Self {
        Self { client }
    }
}

#[async_trait]
impl GlobalEventAdapter for GdeltEventAdapter {
    fn provider_name(&self) -> &'static str {
        "gdelt"
    }

    async fn fetch(
        &self,
        rule: &EventRule,
        since: DateTime<Utc>,
        max_results: usize,
    ) -> Result<AdapterOutcome<NormalizedEventSignal>> {
        let query = build_gdelt_query(rule)?;
        let max_records = max_results.clamp(1, 250).to_string();
        let since = since.format("%Y%m%d%H%M%S").to_string();
        let response = self
            .client
            .get(GDELT_DOC_ENDPOINT)
            .query(&[
                ("query", query.as_str()),
                ("mode", "artlist"),
                ("format", "json"),
                ("sort", "datedesc"),
                ("maxrecords", max_records.as_str()),
                ("startdatetime", since.as_str()),
            ])
            .send()
            .await?;
        let status = response.status();
        let retry_after = response
            .headers()
            .get("retry-after")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok())
            .map(Duration::from_secs);
        if !status.is_success() {
            let status = match status {
                StatusCode::FORBIDDEN => ProviderStatus::Blocked,
                StatusCode::TOO_MANY_REQUESTS => ProviderStatus::RateLimited,
                status if status.is_server_error() => ProviderStatus::TransientError,
                _ => ProviderStatus::PermanentError,
            };
            return Ok(AdapterOutcome::failure(status, retry_after));
        }
        let bytes = response.bytes().await?;
        Ok(parse_gdelt_payload(&bytes, rule, Utc::now()))
    }
}

pub async fn load_rules(pool: &PgPool) -> Result<Vec<EventRule>> {
    let rows = sqlx::query(
        r#"
        SELECT rule.id, rule.symbol, rule.name, rule.countries,
               rule.themes, rule.query_terms
        FROM global_event_rules AS rule
        JOIN tracked_symbols AS stock ON stock.symbol = rule.symbol
        WHERE rule.is_active AND stock.is_active
        ORDER BY rule.symbol, rule.id
        "#,
    )
    .fetch_all(pool)
    .await?;
    Ok(rows
        .into_iter()
        .map(|row| EventRule {
            id: row.get("id"),
            symbol: row.get("symbol"),
            name: row.get("name"),
            countries: row.get::<Json<Vec<String>>, _>("countries").0,
            themes: row.get::<Json<Vec<String>>, _>("themes").0,
            query_terms: row.get::<Json<Vec<String>>, _>("query_terms").0,
        })
        .collect())
}

pub async fn store_rule_events(
    pool: &PgPool,
    rule: &EventRule,
    events: &[NormalizedEventSignal],
    cap_per_day: i64,
) -> Result<u64> {
    if cap_per_day <= 0 || events.is_empty() {
        return Ok(0);
    }
    let days = events
        .iter()
        .map(|event| event.occurred_at.date_naive())
        .collect::<std::collections::HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let rows = sqlx::query(
        r#"
        SELECT (signal.occurred_at AT TIME ZONE 'UTC')::date AS event_day,
               COUNT(*) AS count
        FROM stock_event_links AS link
        JOIN global_event_signals AS signal ON signal.id = link.event_id
        WHERE link.rule_id = $1
          AND (signal.occurred_at AT TIME ZONE 'UTC')::date = ANY($2)
        GROUP BY event_day
        "#,
    )
    .bind(rule.id)
    .bind(&days)
    .fetch_all(pool)
    .await?;
    let mut counts = rows
        .into_iter()
        .map(|row| {
            (
                row.get::<NaiveDate, _>("event_day"),
                row.get::<i64, _>("count"),
            )
        })
        .collect::<std::collections::HashMap<_, _>>();
    let mut transaction = pool.begin().await?;
    let result =
        store_in_transaction(&mut transaction, rule, events, cap_per_day, &mut counts).await;
    match result {
        Ok(linked) => {
            transaction.commit().await?;
            Ok(linked)
        }
        Err(error) => {
            transaction.rollback().await?;
            Err(error)
        }
    }
}

async fn store_in_transaction(
    transaction: &mut Transaction<'_, Postgres>,
    rule: &EventRule,
    events: &[NormalizedEventSignal],
    cap_per_day: i64,
    counts: &mut std::collections::HashMap<NaiveDate, i64>,
) -> Result<u64> {
    let match_reason = json!({
        "rule_id": rule.id,
        "rule_name": rule.name,
        "countries": rule.countries,
        "themes": rule.themes,
        "query_terms": rule.query_terms,
        "provider_query": build_gdelt_query(rule)?,
    });
    let mut ordered = events.to_vec();
    ordered.sort_by_key(|event| event.occurred_at);
    let mut linked = 0;
    for event in ordered {
        let day = event.occurred_at.date_naive();
        if counts.get(&day).copied().unwrap_or(0) >= cap_per_day {
            continue;
        }
        let inserted = sqlx::query_scalar::<_, i64>(
            r#"
            INSERT INTO global_event_signals (
                provider, provider_event_id, canonical_url, title, summary,
                source_name, occurred_at, countries, themes
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT DO NOTHING
            RETURNING id
            "#,
        )
        .bind(&event.provider)
        .bind(&event.provider_event_id)
        .bind(&event.canonical_url)
        .bind(&event.title)
        .bind(&event.summary)
        .bind(&event.source_name)
        .bind(event.occurred_at)
        .bind(Json(&event.countries))
        .bind(Json(&event.themes))
        .fetch_optional(&mut **transaction)
        .await?;
        let event_id = if let Some(id) = inserted {
            id
        } else {
            let id = sqlx::query_scalar::<_, i64>(
                r#"
                SELECT id FROM global_event_signals
                WHERE provider = $1
                  AND (provider_event_id = $2 OR canonical_url = $3)
                ORDER BY CASE WHEN provider_event_id = $2 THEN 0 ELSE 1 END, id
                LIMIT 1
                "#,
            )
            .bind(&event.provider)
            .bind(&event.provider_event_id)
            .bind(&event.canonical_url)
            .fetch_optional(&mut **transaction)
            .await?
            .context("event deduplication conflict was not found")?;
            sqlx::query(
                r#"
                UPDATE global_event_signals SET title=$1, summary=$2,
                    source_name=$3, occurred_at=$4, countries=$5, themes=$6,
                    ingested_at=NOW()
                WHERE id=$7
                "#,
            )
            .bind(&event.title)
            .bind(&event.summary)
            .bind(&event.source_name)
            .bind(event.occurred_at)
            .bind(Json(&event.countries))
            .bind(Json(&event.themes))
            .bind(id)
            .execute(&mut **transaction)
            .await?;
            id
        };
        let was_linked = sqlx::query_scalar::<_, i64>(
            r#"
            INSERT INTO stock_event_links (event_id, symbol, rule_id, match_reason)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (event_id, symbol, rule_id) DO NOTHING
            RETURNING event_id
            "#,
        )
        .bind(event_id)
        .bind(&rule.symbol)
        .bind(rule.id)
        .bind(Json(&match_reason))
        .fetch_optional(&mut **transaction)
        .await?
        .is_some();
        if was_linked {
            linked += 1;
            *counts.entry(day).or_default() += 1;
        }
    }
    Ok(linked)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn rule() -> EventRule {
        EventRule {
            id: 7,
            symbol: "NVDA".into(),
            name: "supply".into(),
            countries: vec!["Taiwan".into()],
            themes: vec!["SUPPLY_CHAIN".into()],
            query_terms: vec!["Nvidia <> chips".into()],
        }
    }

    #[test]
    fn query_is_sanitized_and_deterministic() {
        assert_eq!(
            build_gdelt_query(&rule()).unwrap(),
            r#"("Nvidia chips") AND ("Taiwan") AND (theme:"SUPPLY_CHAIN")"#
        );
    }

    #[test]
    fn url_identity_is_normalized_before_hashing() {
        let a = normalize_url("HTTPS://Example.COM:443/story?b=2&a=1#part").unwrap();
        let b = normalize_url("https://example.com/story?a=1&b=2").unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn parses_and_deduplicates_articles() {
        let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
        let fixture = br#"{"articles":[
          {"url":"https://example.com/a?b=2&a=1","title":"Supply update","domain":"example.com","seendate":"20260822T010203Z"},
          {"url":"https://example.com/a?a=1&b=2#x","title":"Duplicate"}
        ]}"#;
        let outcome = parse_gdelt_payload(fixture, &rule(), now);
        assert_eq!(outcome.status, ProviderStatus::Success);
        assert_eq!(outcome.items.len(), 1);
        assert_eq!(
            outcome.items[0].occurred_at,
            Utc.with_ymd_and_hms(2026, 8, 22, 1, 2, 3).unwrap()
        );
    }

    #[test]
    fn recorded_fixture_matches_normalized_fields() {
        let case: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/providers/gdelt/cases.json"
        ))
        .unwrap();
        let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
        let payload = serde_json::to_vec(&case["responses"]["success"]["body"]).unwrap();
        let outcome = parse_gdelt_payload(&payload, &rule(), now);
        let event = &outcome.items[0];
        assert_eq!(event.canonical_url, case["expected"][0]["canonical_url"]);
        assert_eq!(event.title, case["expected"][0]["title"]);
        assert_eq!(
            event.source_name.as_deref(),
            case["expected"][0]["source_name"].as_str()
        );
        assert_eq!(
            event
                .occurred_at
                .to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            case["expected"][0]["occurred_at"]
        );
    }
}
