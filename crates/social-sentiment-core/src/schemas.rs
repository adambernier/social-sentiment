use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

fn default_engagement() -> i32 {
    1
}

fn default_schema_version() -> i32 {
    1
}

fn default_git_commit() -> String {
    std::env::var("PIPELINE_GIT_COMMIT").unwrap_or_else(|_| "unknown".to_string())
}

fn default_model_meta() -> String {
    "legacy".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RawPost {
    pub id: String,
    pub symbol: String,
    pub platform: String,
    pub text: String,
    pub timestamp: DateTime<Utc>,
    #[serde(default = "default_engagement")]
    pub engagement: i32,
    #[serde(default = "Utc::now")]
    pub ingested_at: DateTime<Utc>,
    #[serde(default = "Utc::now")]
    pub engagement_observed_at: DateTime<Utc>,
    #[serde(default = "default_schema_version")]
    pub source_schema_version: i32,
    #[serde(default = "default_git_commit")]
    pub pipeline_git_commit: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CleanPost {
    pub id: String,
    pub symbol: String,
    pub platform: String,
    pub text: String,
    pub timestamp: DateTime<Utc>,
    pub topic_id: Option<i32>,
    pub topic_label: Option<String>,
    #[serde(default = "default_engagement")]
    pub engagement: i32,
    #[serde(default = "Utc::now")]
    pub ingested_at: DateTime<Utc>,
    #[serde(default = "Utc::now")]
    pub engagement_observed_at: DateTime<Utc>,
    #[serde(default = "default_schema_version")]
    pub source_schema_version: i32,
    #[serde(default = "default_git_commit")]
    pub pipeline_git_commit: String,
    #[serde(default = "Utc::now")]
    pub cleaned_at: DateTime<Utc>,
    #[serde(default = "Utc::now")]
    pub topic_scored_at: DateTime<Utc>,
    #[serde(default = "default_model_meta")]
    pub topic_model_version: String,
    #[serde(default = "default_model_meta")]
    pub topic_model_hash: String,
}

impl CleanPost {
    pub fn from_raw(
        raw: RawPost,
        cleaned_text: String,
        topic_id: Option<i32>,
        topic_label: Option<String>,
        topic_model_version: String,
        topic_model_hash: String,
    ) -> Self {
        let now = Utc::now();
        Self {
            id: raw.id,
            symbol: raw.symbol,
            platform: raw.platform,
            text: cleaned_text,
            timestamp: raw.timestamp,
            topic_id,
            topic_label,
            engagement: raw.engagement,
            ingested_at: raw.ingested_at,
            engagement_observed_at: raw.engagement_observed_at,
            source_schema_version: raw.source_schema_version,
            pipeline_git_commit: raw.pipeline_git_commit,
            cleaned_at: now,
            topic_scored_at: now,
            topic_model_version,
            topic_model_hash,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ScoredPost {
    #[serde(flatten)]
    pub clean: CleanPost,
    pub sentiment: String, // "positive", "neutral", "negative"
    pub scores: HashMap<String, f64>,
    #[serde(default = "Utc::now")]
    pub sentiment_scored_at: DateTime<Utc>,
    #[serde(default = "default_model_meta")]
    pub sentiment_model_version: String,
    #[serde(default = "default_model_meta")]
    pub sentiment_model_hash: String,
}

impl ScoredPost {
    pub fn validate(&self) -> Result<(), String> {
        if !matches!(self.sentiment.as_str(), "positive" | "neutral" | "negative") {
            return Err(format!(
                "sentiment must be positive, neutral, or negative, got {}",
                self.sentiment
            ));
        }

        let required = ["positive", "neutral", "negative"];
        for label in &required {
            if !self.scores.contains_key(*label) {
                return Err(format!("scores must include label: {}", label));
            }
        }

        let pos = self.scores["positive"];
        let neu = self.scores["neutral"];
        let neg = self.scores["negative"];

        for (label, val) in [("positive", pos), ("neutral", neu), ("negative", neg)] {
            if !val.is_finite() || !(0.0..=1.0).contains(&val) {
                return Err(format!(
                    "score for {} must be finite and in [0, 1], got {}",
                    label, val
                ));
            }
        }

        let sum = pos + neu + neg;
        if (sum - 1.0).abs() > 0.0001 {
            return Err(format!(
                "scores sum to {}, which is not 1.0 (abs_tol 0.0001)",
                sum
            ));
        }

        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StockQuote {
    pub symbol: String,
    pub timestamp: DateTime<Utc>,
    pub price: f64,
    pub volume: i64,
    pub market_session: String,
    #[serde(default = "default_provider")]
    pub provider: String,
}

fn default_provider() -> String {
    "yfinance".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct StockMetrics {
    pub symbol: String,
    pub pe_ratio: Option<f64>,
    pub beta: Option<f64>,
    pub avg_return_1y: Option<f64>,
    pub inflation_adj_return_1y: Option<f64>,
    pub pe_relative_sector: Option<f64>,
    pub beta_relative_sector: Option<f64>,
    pub return_relative_sector: Option<f64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_raw_post_serde_defaults() {
        let json_str = r#"{
            "id": "post-123",
            "symbol": "AAPL",
            "platform": "bluesky",
            "text": "Apple announces quarterly results!",
            "timestamp": "2026-08-17T12:00:00Z"
        }"#;

        let raw: RawPost = serde_json::from_str(json_str).expect("deserialize raw post");
        assert_eq!(raw.id, "post-123");
        assert_eq!(raw.engagement, 1);
        assert_eq!(raw.source_schema_version, 1);
        assert_eq!(raw.pipeline_git_commit, "unknown");
    }

    #[test]
    fn test_scored_post_validation() {
        let mut scores = HashMap::new();
        scores.insert("positive".to_string(), 0.7);
        scores.insert("neutral".to_string(), 0.2);
        scores.insert("negative".to_string(), 0.1);

        let clean = CleanPost {
            id: "1".into(),
            symbol: "NVDA".into(),
            platform: "reddit".into(),
            text: "NVDA earnings look promising".into(),
            timestamp: Utc::now(),
            topic_id: Some(0),
            topic_label: Some("Earnings & Guidance".into()),
            engagement: 10,
            ingested_at: Utc::now(),
            engagement_observed_at: Utc::now(),
            source_schema_version: 1,
            pipeline_git_commit: "unknown".into(),
            cleaned_at: Utc::now(),
            topic_scored_at: Utc::now(),
            topic_model_version: "v1".into(),
            topic_model_hash: "abc".into(),
        };

        let mut scored = ScoredPost {
            clean,
            sentiment: "positive".into(),
            scores,
            sentiment_scored_at: Utc::now(),
            sentiment_model_version: "fintwitbert-onnx-v1".into(),
            sentiment_model_hash: "def".into(),
        };

        assert!(scored.validate().is_ok());
        scored.sentiment = "bullish".into();
        assert!(scored.validate().is_err());
    }

    #[test]
    fn test_scored_post_validation_fails_non_summing() {
        let mut scores = HashMap::new();
        scores.insert("positive".to_string(), 0.8);
        scores.insert("neutral".to_string(), 0.2);
        scores.insert("negative".to_string(), 0.3); // sum = 1.3

        let clean = CleanPost {
            id: "1".into(),
            symbol: "NVDA".into(),
            platform: "reddit".into(),
            text: "NVDA earnings".into(),
            timestamp: Utc::now(),
            topic_id: None,
            topic_label: None,
            engagement: 1,
            ingested_at: Utc::now(),
            engagement_observed_at: Utc::now(),
            source_schema_version: 1,
            pipeline_git_commit: "unknown".into(),
            cleaned_at: Utc::now(),
            topic_scored_at: Utc::now(),
            topic_model_version: "v1".into(),
            topic_model_hash: "abc".into(),
        };

        let scored = ScoredPost {
            clean,
            sentiment: "positive".into(),
            scores,
            sentiment_scored_at: Utc::now(),
            sentiment_model_version: "v1".into(),
            sentiment_model_hash: "def".into(),
        };

        let res = scored.validate();
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("scores sum to"));
    }
}
