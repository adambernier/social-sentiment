use std::sync::atomic::{AtomicUsize, Ordering};

use anyhow::{bail, Result};
use async_trait::async_trait;
use chrono::Utc;
use social_news_producer::{publish_then_commit, raw_post, PendingUnit};
use social_sentiment_core::{
    producer::{BoundedCursor, RawPostPublisher},
    schemas::RawPost,
};

struct FailingPublisher {
    fail_at: usize,
    calls: AtomicUsize,
}

#[async_trait]
impl RawPostPublisher for FailingPublisher {
    async fn publish(&self, _post: &RawPost) -> Result<()> {
        let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        if call == self.fail_at {
            bail!("simulated publisher-confirm failure");
        }
        Ok(())
    }
}

#[tokio::test]
async fn cursor_does_not_advance_after_partial_publish_failure() {
    let now = Utc::now();
    let post = |symbol: &str| {
        raw_post(
            "rd-one".into(),
            symbol,
            "reddit",
            "shared comment".into(),
            now,
            1,
            now,
        )
    };
    let unit = PendingUnit {
        posts: vec![post("AAPL"), post("MSFT")],
        cursor_key: "one".into(),
        cursor_value: "one".into(),
    };
    let mut cursor = BoundedCursor::new(10);
    let publisher = FailingPublisher {
        fail_at: 2,
        calls: AtomicUsize::new(0),
    };
    assert!(publish_then_commit(&publisher, &mut cursor, unit)
        .await
        .is_err());
    assert!(!cursor.contains_key(&"one".to_owned()));
}

#[tokio::test]
async fn cursor_advances_after_all_confirms() {
    let now = Utc::now();
    let unit = PendingUnit {
        posts: vec![raw_post(
            "one".into(),
            "AAPL",
            "reddit",
            "comment".into(),
            now,
            1,
            now,
        )],
        cursor_key: "one".into(),
        cursor_value: "one".into(),
    };
    let mut cursor = BoundedCursor::new(10);
    let publisher = FailingPublisher {
        fail_at: usize::MAX,
        calls: AtomicUsize::new(0),
    };
    publish_then_commit(&publisher, &mut cursor, unit)
        .await
        .unwrap();
    assert!(cursor.contains_key(&"one".to_owned()));
}
