use chrono::{DateTime, Utc};
use serde::Deserialize;
use social_sentiment_core::producer::{AdapterOutcome, ProviderStatus};

use crate::{parse_timestamp, raw_post, PendingUnit};

#[derive(Deserialize)]
struct Response {
    #[serde(default)]
    messages: Vec<Message>,
}

#[derive(Deserialize)]
struct Message {
    id: i64,
    body: String,
    created_at: String,
    #[serde(default)]
    likes: Option<Likes>,
}

#[derive(Deserialize)]
struct Likes {
    #[serde(default)]
    total: i32,
}

pub fn parse(payload: &[u8], symbol: &str, now: DateTime<Utc>) -> AdapterOutcome<PendingUnit> {
    let response: Response = match serde_json::from_slice(payload) {
        Ok(response) => response,
        Err(error) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
            outcome.detail = Some(format!("invalid StockTwits response: {error}"));
            return outcome;
        }
    };
    let items = response
        .messages
        .into_iter()
        .rev()
        .map(|message| {
            let id = message.id.to_string();
            PendingUnit {
                posts: vec![raw_post(
                    format!("st_{id}"),
                    symbol,
                    "stocktwits",
                    message.body,
                    parse_timestamp(Some(&message.created_at), now),
                    message.likes.map_or(0, |likes| likes.total),
                    now,
                )],
                cursor_key: symbol.to_owned(),
                cursor_value: id,
            }
        })
        .collect();
    AdapterOutcome::success(items)
}
