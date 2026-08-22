use chrono::{DateTime, Utc};
use serde::Deserialize;
use social_sentiment_core::{
    producer::{AdapterOutcome, ProviderStatus},
    symbols::SymbolConfig,
};

use crate::{parse_timestamp, raw_post, PendingUnit};

#[derive(Deserialize)]
struct SearchResponse {
    #[serde(default)]
    posts: Vec<Post>,
}

#[derive(Deserialize)]
struct Post {
    cid: String,
    record: Record,
    #[serde(default, rename = "likeCount")]
    like_count: i32,
    #[serde(default, rename = "repostCount")]
    repost_count: i32,
    #[serde(default, rename = "replyCount")]
    reply_count: i32,
}

#[derive(Deserialize)]
struct Record {
    text: String,
    #[serde(rename = "createdAt")]
    created_at: String,
}

pub fn parse(
    payload: &[u8],
    symbol: &SymbolConfig,
    term: &str,
    since: Option<&str>,
    now: DateTime<Utc>,
) -> AdapterOutcome<PendingUnit> {
    let response: SearchResponse = match serde_json::from_slice(payload) {
        Ok(response) => response,
        Err(error) => {
            let mut outcome = AdapterOutcome::failure(ProviderStatus::TransientError, None);
            outcome.detail = Some(format!("invalid Bluesky response: {error}"));
            return outcome;
        }
    };
    let matched = response
        .posts
        .into_iter()
        .filter(|post| since.is_none_or(|cursor| post.record.created_at.as_str() > cursor))
        .filter(|post| symbol.matches(&post.record.text))
        .map(|post| {
            let engagement = post.like_count + 2 * post.repost_count + 3 * post.reply_count;
            let cursor = post.record.created_at.clone();
            (
                raw_post(
                    post.cid,
                    &symbol.symbol,
                    "bluesky",
                    post.record.text,
                    parse_timestamp(Some(&cursor), now),
                    engagement,
                    now,
                ),
                cursor,
            )
        })
        .collect::<Vec<_>>();
    let items = matched
        .iter()
        .map(|(_, cursor)| cursor)
        .max()
        .cloned()
        .map(|cursor_value| PendingUnit {
            posts: matched.into_iter().map(|(post, _)| post).collect(),
            cursor_key: term.to_owned(),
            cursor_value,
        })
        .into_iter()
        .collect();
    AdapterOutcome::success(items)
}
