use regex::Regex;
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

impl SymbolConfig {
    /// Match the precision rules via the Kani-verified precedence policy:
    /// block phrases win, then cashtags/keywords, followed by a bare ticker
    /// match gated by `require_cashtag` and `require_uppercase`.
    pub fn matches(&self, text: &str) -> bool {
        let lowered = text.to_lowercase();
        let has_block_phrase = self
            .block_phrases
            .iter()
            .any(|phrase| lowered.contains(&phrase.to_lowercase()));

        let boundary_match = |needle: &str, case_insensitive: bool| {
            let flags = if case_insensitive { "(?i)" } else { "" };
            let pattern = format!(
                "{flags}(?:^|[^A-Za-z0-9]){}(?:$|[^A-Za-z0-9])",
                regex::escape(needle)
            );
            Regex::new(&pattern).is_ok_and(|regex| regex.is_match(text))
        };

        let has_cashtag = !has_block_phrase && boundary_match(&format!("${}", self.symbol), true);
        let has_keyword = !has_block_phrase
            && !has_cashtag
            && self
                .keywords
                .iter()
                .any(|keyword| boundary_match(keyword, true));
        let need_bare = !has_block_phrase && !has_cashtag && !has_keyword && !self.require_cashtag;
        let has_bare_ticker_upper =
            need_bare && self.require_uppercase && boundary_match(&self.symbol, false);
        let has_bare_ticker_ci =
            need_bare && !self.require_uppercase && boundary_match(&self.symbol, true);

        social_sentiment_math::symbols::evaluate_symbol_match(
            social_sentiment_math::symbols::SymbolMatchPolicy {
                require_cashtag: self.require_cashtag,
                require_uppercase: self.require_uppercase,
            },
            social_sentiment_math::symbols::SymbolMatchSignals {
                has_block_phrase,
                has_cashtag,
                has_keyword,
                has_bare_ticker_ci,
                has_bare_ticker_upper,
            },
        )
    }

    pub fn search_terms(&self) -> Vec<String> {
        let mut terms = vec![self.symbol.clone(), format!("${}", self.symbol)];
        terms.extend(self.keywords.iter().cloned());
        terms
    }
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

    #[test]
    fn symbol_match_preserves_precision_controls() {
        let config = SymbolConfig {
            symbol: "MU".into(),
            keywords: vec!["Micron".into()],
            future: None,
            sector: None,
            require_uppercase: true,
            block_phrases: vec!["Manchester United".into()],
            require_cashtag: false,
        };
        assert!(config.matches("$mu and Micron rallied"));
        assert!(config.matches("MU rallied"));
        assert!(!config.matches("mu is a Greek letter"));
        assert!(!config.matches("Manchester United ($MU) won"));
    }
}
