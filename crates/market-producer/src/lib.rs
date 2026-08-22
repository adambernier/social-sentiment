pub mod adapters;
pub mod calculations;
pub mod runtime;
pub mod sessions;
pub mod synthetic_nav;

use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use social_sentiment_core::{
    producer::AdapterOutcome,
    repository::GlobalMarketBar,
    schemas::{StockMetrics, StockQuote},
};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MarketInstrument {
    pub instrument_key: String,
    pub display_name: String,
    pub asset_class: String,
    pub currency: String,
    pub exchange: Option<String>,
    pub timezone: String,
    pub provider_aliases: serde_json::Map<String, serde_json::Value>,
    pub session_metadata: serde_json::Map<String, serde_json::Value>,
    pub quote_convention: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DailyClose {
    pub date: NaiveDate,
    pub close: f64,
}

#[async_trait]
pub trait QuoteProvider: Send + Sync {
    fn provider_name(&self) -> &'static str;
    async fn fetch_quote(
        &self,
        symbol: &str,
        session: &str,
    ) -> anyhow::Result<AdapterOutcome<StockQuote>>;
}

#[async_trait]
pub trait FundamentalsProvider: Send + Sync {
    async fn fetch_fundamentals(
        &self,
        symbol: &str,
        sector_baseline: &str,
    ) -> anyhow::Result<AdapterOutcome<StockMetrics>>;
}

#[async_trait]
pub trait GlobalBarProvider: Send + Sync {
    fn provider_name_for(
        &self,
        instrument: &MarketInstrument,
        interval: &str,
    ) -> Option<&'static str>;

    async fn fetch_bars(
        &self,
        instrument: &MarketInstrument,
        interval: &str,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> anyhow::Result<AdapterOutcome<GlobalMarketBar>>;
}

pub trait SessionCalendar: Send + Sync {
    fn equity_session(&self, now: DateTime<Utc>) -> String;
    fn futures_session(&self, symbol: &str, now: DateTime<Utc>) -> String;
    fn is_trading_day(&self, date: NaiveDate) -> bool;
    fn previous_trading_day(&self, date: NaiveDate) -> Option<NaiveDate>;
}
