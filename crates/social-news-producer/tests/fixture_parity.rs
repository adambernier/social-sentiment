use chrono::{TimeZone, Utc};
use serde_json::{json, Value};
use social_news_producer::adapters;
use social_sentiment_core::{producer::ProviderStatus, schemas::RawPost, symbols::SymbolConfig};

fn fixture(provider: &str) -> Value {
    let raw = match provider {
        "bluesky" => include_str!("../../../tests/fixtures/providers/bluesky/cases.json"),
        "stocktwits" => include_str!("../../../tests/fixtures/providers/stocktwits/cases.json"),
        "reddit" => include_str!("../../../tests/fixtures/providers/reddit/cases.json"),
        "finnhub" => include_str!("../../../tests/fixtures/providers/finnhub/cases.json"),
        "alpaca" => include_str!("../../../tests/fixtures/providers/alpaca/cases.json"),
        _ => panic!("unknown fixture"),
    };
    serde_json::from_str(raw).unwrap()
}

fn symbol() -> SymbolConfig {
    SymbolConfig {
        symbol: "AAPL".into(),
        keywords: vec!["Apple".into()],
        future: None,
        sector: Some("XLK".into()),
        require_uppercase: false,
        block_phrases: Vec::new(),
        require_cashtag: false,
    }
}

fn body(value: &Value, scenario: &str) -> Vec<u8> {
    serde_json::to_vec(&value["responses"][scenario]["body"]).unwrap()
}

fn normalize(post: &RawPost) -> Value {
    json!({
        "id": post.id,
        "symbol": post.symbol,
        "platform": post.platform,
        "text": post.text,
        "timestamp": post.timestamp,
        "engagement": post.engagement,
    })
}

fn posts(items: Vec<social_news_producer::PendingUnit>) -> Vec<Value> {
    items
        .into_iter()
        .flat_map(|item| item.posts)
        .map(|post| normalize(&post))
        .collect()
}

#[test]
fn social_success_fixtures_match_normalized_oracle() {
    let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
    for provider in ["bluesky", "stocktwits", "reddit", "finnhub", "alpaca"] {
        let case = fixture(provider);
        let payload = body(&case, "success");
        let outcome = match provider {
            "bluesky" => adapters::bluesky::parse(&payload, &symbol(), "Apple", None, now),
            "stocktwits" => adapters::stocktwits::parse(&payload, "AAPL", now),
            "reddit" => adapters::reddit::parse(&payload, &[symbol()], |_| false, now),
            "finnhub" => adapters::finnhub::parse(&payload, &symbol(), |_| false, now),
            "alpaca" => adapters::alpaca::parse(&payload, &symbol(), |_| false, now),
            _ => unreachable!(),
        };
        assert_eq!(outcome.status, ProviderStatus::Success, "{provider}");
        assert_eq!(
            Value::Array(posts(outcome.items)),
            case["expected"],
            "{provider}"
        );
    }
}

#[test]
fn empty_and_malformed_fixtures_have_explicit_outcomes() {
    let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
    let case = fixture("reddit");
    assert_eq!(
        adapters::reddit::parse(&body(&case, "empty"), &[symbol()], |_| false, now).status,
        ProviderStatus::NoData
    );
    assert_eq!(
        adapters::reddit::parse(&body(&case, "malformed"), &[symbol()], |_| false, now).status,
        ProviderStatus::TransientError
    );
}

#[test]
fn cursor_units_retain_publish_before_commit_boundaries() {
    let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
    let case = fixture("reddit");
    let outcome = adapters::reddit::parse(&body(&case, "success"), &[symbol()], |_| false, now);
    assert_eq!(outcome.items.len(), 2);
    assert_eq!(outcome.items[0].posts.len(), 1);
    assert!(outcome.items[1].posts.is_empty());
    assert_eq!(outcome.items[1].cursor_key, "empty");
}
