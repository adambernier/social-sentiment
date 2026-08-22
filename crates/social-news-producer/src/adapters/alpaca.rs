use chrono::{DateTime, Utc};
use serde_json::Value;
use social_sentiment_core::{
    producer::{AdapterOutcome, ProviderStatus},
    symbols::SymbolConfig,
};

use crate::{parse_timestamp, raw_post, PendingUnit};

pub fn parse(
    payload: &[u8],
    symbol: &SymbolConfig,
    seen: impl Fn(&str) -> bool,
    now: DateTime<Utc>,
) -> AdapterOutcome<PendingUnit> {
    let root: Value = match serde_json::from_slice(payload) {
        Ok(root) => root,
        Err(error) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::PermanentError, None);
            outcome.detail = Some(format!("invalid Alpaca response: {error}"));
            return outcome;
        }
    };
    let Some(articles) = root.get("news").and_then(Value::as_array) else {
        let mut outcome = AdapterOutcome::failure(ProviderStatus::PermanentError, None);
        outcome.detail = Some("invalid Alpaca news array".into());
        return outcome;
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
        let post_id = format!("alpaca_{id}");
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
            vec![raw_post(
                post_id.clone(),
                &symbol.symbol,
                "alpaca",
                text,
                parse_timestamp(article.get("created_at").and_then(Value::as_str), now),
                1,
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
