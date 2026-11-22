use once_cell::sync::Lazy;
use regex::{Regex, RegexSet};
use serde::{Deserialize, Serialize};
use social_sentiment_math::symbols::{
    evaluate_symbol_match, SymbolMatchPolicy, SymbolMatchSignals,
};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

const MAX_MATCHER_CACHE_ENTRIES: usize = 512;

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

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct MatcherCacheKey {
    symbol: String,
    keywords: Vec<String>,
    block_phrases: Vec<String>,
    require_uppercase: bool,
    require_cashtag: bool,
}

impl MatcherCacheKey {
    fn from_config(config: &SymbolConfig) -> Self {
        Self {
            symbol: config.symbol.clone(),
            keywords: config.keywords.clone(),
            block_phrases: config.block_phrases.clone(),
            require_uppercase: config.require_uppercase,
            require_cashtag: config.require_cashtag,
        }
    }
}

/// Precompiled matcher for a `SymbolConfig`, avoiding per-call regex compilation
/// and unnecessary string allocations across high-volume producer streams.
#[derive(Debug)]
pub struct CompiledSymbolMatcher {
    policy: SymbolMatchPolicy,
    block_phrases_lower: Vec<String>,
    cashtag_regex: Option<Regex>,
    keyword_regexes: Option<RegexSet>,
    bare_upper_regex: Option<Regex>,
    bare_ci_regex: Option<Regex>,
}

fn boundary_pattern(needle: &str, case_insensitive: bool) -> String {
    let flags = if case_insensitive { "(?i)" } else { "" };
    format!(
        "{flags}(?:^|[^A-Za-z0-9]){}(?:$|[^A-Za-z0-9])",
        regex::escape(needle)
    )
}

impl CompiledSymbolMatcher {
    pub fn new(config: &SymbolConfig) -> Self {
        let block_phrases_lower = config
            .block_phrases
            .iter()
            .map(|phrase| phrase.to_lowercase())
            .collect();

        let cashtag_regex =
            Regex::new(&boundary_pattern(&format!("${}", config.symbol), true)).ok();

        let keyword_regexes = if config.keywords.is_empty() {
            None
        } else {
            let patterns = config
                .keywords
                .iter()
                .map(|keyword| boundary_pattern(keyword, true));
            RegexSet::new(patterns).ok()
        };

        let bare_upper_regex = (!config.require_cashtag && config.require_uppercase)
            .then(|| Regex::new(&boundary_pattern(&config.symbol, false)).ok())
            .flatten();

        let bare_ci_regex = (!config.require_cashtag && !config.require_uppercase)
            .then(|| Regex::new(&boundary_pattern(&config.symbol, true)).ok())
            .flatten();

        Self {
            policy: SymbolMatchPolicy {
                require_cashtag: config.require_cashtag,
                require_uppercase: config.require_uppercase,
            },
            block_phrases_lower,
            cashtag_regex,
            keyword_regexes,
            bare_upper_regex,
            bare_ci_regex,
        }
    }

    pub fn matches(&self, text: &str) -> bool {
        let has_block_phrase = !self.block_phrases_lower.is_empty() && {
            let lowered = text.to_lowercase();
            self.block_phrases_lower
                .iter()
                .any(|phrase| lowered.contains(phrase.as_str()))
        };

        let has_cashtag = !has_block_phrase
            && self
                .cashtag_regex
                .as_ref()
                .is_some_and(|regex| regex.is_match(text));
        let has_keyword = !has_block_phrase
            && !has_cashtag
            && self
                .keyword_regexes
                .as_ref()
                .is_some_and(|set| set.is_match(text));
        let need_bare =
            !has_block_phrase && !has_cashtag && !has_keyword && !self.policy.require_cashtag;
        let has_bare_ticker_upper = need_bare
            && self.policy.require_uppercase
            && self
                .bare_upper_regex
                .as_ref()
                .is_some_and(|regex| regex.is_match(text));
        let has_bare_ticker_ci = need_bare
            && !self.policy.require_uppercase
            && self
                .bare_ci_regex
                .as_ref()
                .is_some_and(|regex| regex.is_match(text));

        evaluate_symbol_match(
            self.policy,
            SymbolMatchSignals {
                has_block_phrase,
                has_cashtag,
                has_keyword,
                has_bare_ticker_ci,
                has_bare_ticker_upper,
            },
        )
    }
}

static MATCHER_CACHE: Lazy<RwLock<HashMap<MatcherCacheKey, Arc<CompiledSymbolMatcher>>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

pub fn default_tracked_tickers() -> Vec<&'static str> {
    vec![
        "INTC", "NVDA", "AMD", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NFLX",
    ]
}

impl SymbolConfig {
    /// Return a cached, precompiled matcher for this symbol's precision rules.
    pub fn matcher(&self) -> Arc<CompiledSymbolMatcher> {
        let key = MatcherCacheKey::from_config(self);
        if let Ok(guard) = MATCHER_CACHE.read() {
            if let Some(matcher) = guard.get(&key) {
                return Arc::clone(matcher);
            }
        }
        let compiled = Arc::new(CompiledSymbolMatcher::new(self));
        if let Ok(mut guard) = MATCHER_CACHE.write() {
            if guard.len() >= MAX_MATCHER_CACHE_ENTRIES {
                guard.clear();
            }
            return Arc::clone(guard.entry(key).or_insert(compiled));
        }
        compiled
    }

    /// Match the precision rules via the Kani-verified precedence policy:
    /// block phrases win, then cashtags/keywords, followed by a bare ticker
    /// match gated by `require_cashtag` and `require_uppercase`.
    pub fn matches(&self, text: &str) -> bool {
        self.matcher().matches(text)
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

    #[test]
    fn symbol_matcher_reuses_cached_compiled_instance_and_handles_cashtag_only() {
        let config = SymbolConfig {
            symbol: "ON".into(),
            keywords: vec!["onsemi".into(), "ON Semiconductor".into()],
            future: None,
            sector: None,
            require_uppercase: false,
            block_phrases: Vec::new(),
            require_cashtag: true,
        };
        let first = config.matcher();
        let second = config.matcher();
        assert!(Arc::ptr_eq(&first, &second));
        assert!(first.matches("Watching $ON into earnings"));
        assert!(first.matches("onsemi expands fab capacity"));
        assert!(!first.matches("turned on the lights"));
    }
}
