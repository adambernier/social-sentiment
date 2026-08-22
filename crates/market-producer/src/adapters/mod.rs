pub mod taiwan;
pub mod yahoo;

use crate::{GlobalBarProvider, MarketInstrument};
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use social_sentiment_core::{producer::AdapterOutcome, repository::GlobalMarketBar};

pub struct RoutedGlobalBarProvider {
    pub taiwan: taiwan::TaiwanIndexProvider,
    pub yahoo: yahoo::YahooProvider,
}

#[async_trait]
impl GlobalBarProvider for RoutedGlobalBarProvider {
    fn provider_name_for(
        &self,
        instrument: &MarketInstrument,
        interval: &str,
    ) -> Option<&'static str> {
        if interval == "1d" && instrument.provider_aliases.contains_key("taiwan_index") {
            Some("taiwan_index")
        } else if instrument.provider_aliases.contains_key("yahoo") {
            Some("yahoo")
        } else {
            None
        }
    }

    async fn fetch_bars(
        &self,
        instrument: &MarketInstrument,
        interval: &str,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> anyhow::Result<AdapterOutcome<GlobalMarketBar>> {
        if interval == "1d" && instrument.provider_aliases.contains_key("taiwan_index") {
            self.taiwan.fetch_bars(instrument, start, end).await
        } else {
            self.yahoo
                .fetch_global_bars(instrument, interval, start, end)
                .await
        }
    }
}
