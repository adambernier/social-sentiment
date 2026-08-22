pub mod adapters;
pub mod runtime;

use anyhow::Result;
use chrono::{DateTime, Utc};
use social_sentiment_core::{
    producer::{BoundedCursor, RawPostPublisher},
    schemas::RawPost,
};

#[derive(Debug, Clone, PartialEq)]
pub struct PendingUnit {
    /// All messages must be confirmed before this unit's cursor is committed.
    pub posts: Vec<RawPost>,
    pub cursor_key: String,
    pub cursor_value: String,
}

pub fn raw_post(
    id: String,
    symbol: &str,
    platform: &str,
    text: String,
    timestamp: DateTime<Utc>,
    engagement: i32,
    observed_at: DateTime<Utc>,
) -> RawPost {
    RawPost {
        id,
        symbol: symbol.to_owned(),
        platform: platform.to_owned(),
        text,
        timestamp,
        engagement: engagement.max(1),
        ingested_at: observed_at,
        engagement_observed_at: observed_at,
        source_schema_version: 1,
        pipeline_git_commit: std::env::var("PIPELINE_GIT_COMMIT")
            .unwrap_or_else(|_| "unknown".to_owned()),
    }
}

pub fn parse_timestamp(value: Option<&str>, fallback: DateTime<Utc>) -> DateTime<Utc> {
    value
        .and_then(|raw| DateTime::parse_from_rfc3339(raw).ok())
        .map(|timestamp| timestamp.with_timezone(&Utc))
        .unwrap_or(fallback)
}

/// Publish one source unit atomically with respect to local progress. RabbitMQ
/// may contain a confirmed prefix after a later publish fails, but the cursor
/// remains retryable and downstream identity keys suppress that safe replay.
pub async fn publish_then_commit(
    publisher: &dyn RawPostPublisher,
    cursor: &mut BoundedCursor<String, String>,
    unit: PendingUnit,
) -> Result<usize> {
    for post in &unit.posts {
        publisher.publish(post).await?;
    }
    let published = unit.posts.len();
    cursor.commit(unit.cursor_key, unit.cursor_value);
    Ok(published)
}
