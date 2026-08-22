use chrono::{DateTime, TimeZone, Utc};
use serde_json::Value;
use social_sentiment_core::{
    producer::{AdapterOutcome, ProviderStatus},
    symbols::SymbolConfig,
};

use crate::{raw_post, PendingUnit};

pub fn parse(
    payload: &[u8],
    symbol: &SymbolConfig,
    seen: impl Fn(&str) -> bool,
    now: DateTime<Utc>,
) -> AdapterOutcome<PendingUnit> {
    let articles: Vec<Value> = match serde_json::from_slice(payload) {
        Ok(articles) => articles,
        Err(error) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::PermanentError, None);
            outcome.detail = Some(format!("invalid Finnhub response: {error}"));
            return outcome;
        }
    };
    let mut items = Vec::new();
    for article in articles {
        let Some(raw_id) = article.get("id") else {
            continue;
        };
        let Some(id) = raw_id
            .as_str()
            .map(str::to_owned)
            .or_else(|| raw_id.as_i64().map(|id| id.to_string()))
        else {
            continue;
        };
        let post_id = format!("finnhub_{id}");
        if seen(&post_id) {
            continue;
        }
        let headline = article
            .get("headline")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let summary = article
            .get("summary")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let text = if summary.is_empty() {
            headline.to_owned()
        } else {
            format!("{headline}. {summary}")
        };
        if text.trim().is_empty() {
            continue;
        }
        let posts = if symbol.matches(&text) {
            let timestamp = article
                .get("datetime")
                .and_then(|value| value.as_i64().or_else(|| value.as_str()?.parse().ok()))
                .and_then(|seconds| Utc.timestamp_opt(seconds, 0).single())
                .unwrap_or(now);
            vec![raw_post(
                post_id.clone(),
                &symbol.symbol,
                "finnhub",
                text,
                timestamp,
                15,
                now,
            )]
        } else {
            Vec::new()
        };
        items.push(PendingUnit {
            posts,
            cursor_key: post_id.clone(),
            cursor_value: post_id,
        });
    }
    AdapterOutcome::success(items)
}
