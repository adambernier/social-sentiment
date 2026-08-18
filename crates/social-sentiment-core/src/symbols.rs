use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SymbolConfig {
    pub symbol: String,
    pub keywords: Vec<String>,
    pub future: Option<String>,
    pub sector: Option<String>,
    pub require_uppercase: bool,
    pub block_phrases: Vec<String>,
    pub require_cashtag: bool,
}

pub fn default_tracked_tickers() -> Vec<&'static str> {
    vec![
        "INTC", "NVDA", "AMD", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NFLX",
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_tracked_tickers() {
        let tickers = default_tracked_tickers();
        assert!(tickers.contains(&"INTC"));
        assert!(tickers.contains(&"NVDA"));
    }
}
