use chrono::{DateTime, TimeZone, Utc};
use serde_json::Value;
use social_sentiment_core::{
    producer::{AdapterOutcome, ProviderStatus},
    symbols::SymbolConfig,
};

use crate::{raw_post, PendingUnit};

pub fn parse(
    payload: &[u8],
    symbols: &[SymbolConfig],
    seen: impl Fn(&str) -> bool,
    now: DateTime<Utc>,
) -> AdapterOutcome<PendingUnit> {
    let root: Value = match serde_json::from_slice(payload) {
        Ok(Value::Object(root)) => Value::Object(root),
        Ok(_) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
            outcome.detail = Some("invalid Reddit response root".into());
            return outcome;
        }
        Err(error) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
            outcome.detail = Some(format!("invalid Reddit JSON: {error}"));
            return outcome;
        }
    };
    let Some(children) = root
        .get("data")
        .and_then(Value::as_object)
        .and_then(|data| data.get("children"))
        .and_then(Value::as_array)
    else {
        let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
        outcome.detail = Some("invalid Reddit children".into());
        return outcome;
    };

    let mut items = Vec::new();
    for child in children {
        let Some(comment) = child.get("data").and_then(Value::as_object) else {
            continue;
        };
        let Some(id) = comment
            .get("id")
            .and_then(|value| {
                value
                    .as_str()
                    .map(str::to_owned)
                    .or_else(|| value.as_i64().map(|id| id.to_string()))
            })
            .filter(|id| !id.is_empty())
        else {
            continue;
        };
        if seen(&id) {
            continue;
        }
        let body = comment.get("body").and_then(Value::as_str).unwrap_or("");
        let timestamp = comment
            .get("created_utc")
            .and_then(Value::as_f64)
            .and_then(|seconds| Utc.timestamp_opt(seconds as i64, 0).single())
            .unwrap_or(now);
        let engagement = comment
            .get("score")
            .and_then(|value| value.as_i64().or_else(|| value.as_str()?.parse().ok()))
            .and_then(|score| i32::try_from(score).ok())
            .unwrap_or(1);
        let text = body.trim().chars().take(2_000).collect::<String>();
        let posts = if text.is_empty() {
            Vec::new()
        } else {
            symbols
                .iter()
                .filter(|symbol| symbol.matches(body))
                .map(|symbol| {
                    raw_post(
                        format!("rd_{id}"),
                        &symbol.symbol,
                        "reddit",
                        text.clone(),
                        timestamp,
                        engagement,
                        now,
                    )
                })
                .collect()
        };
        items.push(PendingUnit {
            posts,
            cursor_key: id.clone(),
            cursor_value: id,
        });
    }
    AdapterOutcome::success(items)
}
