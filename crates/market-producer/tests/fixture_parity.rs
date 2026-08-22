use market_producer::{
    adapters::{taiwan, yahoo},
    MarketInstrument,
};
use serde_json::{json, Value};

fn fixture(path: &str) -> Value {
    serde_json::from_str(path).unwrap()
}

#[test]
fn yahoo_recorded_fixture_matches_expected_bar() {
    let case = fixture(include_str!(
        "../../../tests/fixtures/providers/yahoo/cases.json"
    ));
    let bars = yahoo::parse_chart(&case["responses"]["success"]["body"]).unwrap();
    let normalized = bars
        .into_iter()
        .map(|bar| {
            json!({
                "timestamp": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(Value::Array(normalized), case["expected"]);
}

#[test]
fn taiwan_recorded_fixture_matches_official_close_shape() {
    let case = fixture(include_str!(
        "../../../tests/fixtures/providers/taiwan-index/cases.json"
    ));
    let instrument = MarketInstrument {
        instrument_key: "index:taiwan-50".into(),
        display_name: "Taiwan 50".into(),
        asset_class: "index".into(),
        currency: "TWD".into(),
        exchange: Some("TWSE".into()),
        timezone: "Asia/Taipei".into(),
        provider_aliases: serde_json::from_value(json!({"taiwan_index":"TW50"})).unwrap(),
        session_metadata: serde_json::from_value(json!({"open":"09:00","close":"13:30"})).unwrap(),
        quote_convention: None,
    };
    let outcome =
        taiwan::parse_payload(&case["responses"]["success"]["body"], &instrument).unwrap();
    assert_eq!(
        outcome.items[0].ends_at.to_rfc3339(),
        "2026-08-21T05:30:00+00:00"
    );
    let normalized = outcome
        .items
        .into_iter()
        .map(|bar| {
            json!({
                "session_date": bar.session_date,
                "open": bar.open_price,
                "high": bar.high_price,
                "low": bar.low_price,
                "close": bar.close_price,
                "volume": bar.volume,
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(Value::Array(normalized), case["expected"]);
}
