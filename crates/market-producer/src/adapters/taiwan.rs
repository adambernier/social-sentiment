use std::time::Duration;

use anyhow::{bail, Context, Result};
use chrono::{DateTime, NaiveDate, NaiveTime, TimeDelta, TimeZone, Utc};
use chrono_tz::Tz;
use reqwest::{Client, StatusCode};
use serde_json::Value;
use social_sentiment_core::{
    producer::{AdapterOutcome, ProviderStatus},
    repository::GlobalMarketBar,
};

use crate::MarketInstrument;

#[derive(Clone)]
pub struct TaiwanIndexProvider {
    client: Client,
    base_url: String,
}

impl TaiwanIndexProvider {
    pub fn new(client: Client) -> Self {
        Self {
            client,
            base_url: "https://backend.taiwanindex.com.tw/api".into(),
        }
    }

    pub fn with_base(client: Client, base_url: String) -> Self {
        Self { client, base_url }
    }

    pub async fn fetch_bars(
        &self,
        instrument: &MarketInstrument,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<AdapterOutcome<GlobalMarketBar>> {
        let Some(code) = instrument
            .provider_aliases
            .get("taiwan_index")
            .and_then(Value::as_str)
        else {
            return Ok(AdapterOutcome::failure(
                ProviderStatus::PermanentError,
                None,
            ));
        };
        if !code
            .chars()
            .all(|character| character.is_ascii_uppercase() || character.is_ascii_digit())
        {
            bail!("invalid Taiwan Index code");
        }
        let timezone: Tz = instrument
            .timezone
            .parse()
            .context("invalid instrument timezone")?;
        let response = self
            .client
            .get(format!(
                "{}/indexes/{code}/records",
                self.base_url.trim_end_matches('/')
            ))
            .query(&[
                (
                    "start",
                    start.with_timezone(&timezone).date_naive().to_string(),
                ),
                ("end", end.with_timezone(&timezone).date_naive().to_string()),
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
            return Ok(AdapterOutcome::failure(
                match status {
                    StatusCode::TOO_MANY_REQUESTS => ProviderStatus::RateLimited,
                    status if status.is_server_error() => ProviderStatus::TransientError,
                    _ => ProviderStatus::PermanentError,
                },
                retry_after,
            ));
        }
        let payload: Value = response.json().await?;
        parse_payload(&payload, instrument)
    }
}

pub fn parse_payload(
    payload: &Value,
    instrument: &MarketInstrument,
) -> Result<AdapterOutcome<GlobalMarketBar>> {
    if payload.get("empty").and_then(Value::as_bool) == Some(true) {
        return Ok(AdapterOutcome::success(Vec::new()));
    }
    let labels = payload
        .pointer("/data/labels")
        .and_then(Value::as_array)
        .context("Taiwan Index response missing labels")?;
    let datasets = payload
        .pointer("/data/datasets")
        .and_then(Value::as_array)
        .context("Taiwan Index response missing datasets")?;
    let prices = datasets
        .iter()
        .find(|dataset| dataset.get("value_type").and_then(Value::as_str) == Some("price"))
        .and_then(|dataset| dataset.get("data"))
        .and_then(Value::as_array)
        .context("Taiwan Index response missing prices")?;
    if labels.len() != prices.len() {
        bail!("Taiwan Index response has mismatched dates and prices");
    }
    let timezone: Tz = instrument
        .timezone
        .parse()
        .context("invalid instrument timezone")?;
    let open = session_time(instrument, "open", NaiveTime::MIN);
    let close = session_time(
        instrument,
        "close",
        NaiveTime::from_hms_opt(23, 59, 0).unwrap(),
    );
    let mut bars = Vec::new();
    for (label, price) in labels.iter().zip(prices) {
        let Some(label) = label.as_str() else {
            continue;
        };
        let Ok(date) = NaiveDate::parse_from_str(&label.replace('/', "-"), "%Y-%m-%d") else {
            continue;
        };
        let Some(price) = price.as_f64().or_else(|| price.as_str()?.parse().ok()) else {
            continue;
        };
        if !price.is_finite() || price <= 0.0 {
            continue;
        }
        let start_date = if open > close {
            date - TimeDelta::days(1)
        } else {
            date
        };
        let starts_at = local_to_utc(timezone, start_date, open)?;
        let ends_at = local_to_utc(timezone, date, close)?;
        bars.push(GlobalMarketBar {
            instrument_key: instrument.instrument_key.clone(),
            interval: "1d".into(),
            starts_at,
            ends_at,
            session_date: date,
            open_price: price,
            high_price: price,
            low_price: price,
            close_price: price,
            volume: None,
            provider: "taiwan_index".into(),
        });
    }
    Ok(AdapterOutcome::success(bars))
}

fn session_time(instrument: &MarketInstrument, key: &str, fallback: NaiveTime) -> NaiveTime {
    instrument
        .session_metadata
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| NaiveTime::parse_from_str(value, "%H:%M").ok())
        .unwrap_or(fallback)
}

fn local_to_utc(timezone: Tz, date: NaiveDate, time: NaiveTime) -> Result<DateTime<Utc>> {
    timezone
        .from_local_datetime(&date.and_time(time))
        .single()
        .map(|value| value.with_timezone(&Utc))
        .context("ambiguous or nonexistent Taiwan session time")
}
