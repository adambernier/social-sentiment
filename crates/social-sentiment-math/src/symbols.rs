//! Pure symbol-matching precedence policy verified across all boolean combinations.

/// Configured precision policy for a tracked stock symbol.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SymbolMatchPolicy {
    pub require_cashtag: bool,
    pub require_uppercase: bool,
}

/// Extracted text-match signals for a candidate post.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SymbolMatchSignals {
    pub has_block_phrase: bool,
    pub has_cashtag: bool,
    pub has_keyword: bool,
    pub has_bare_ticker_ci: bool,
    pub has_bare_ticker_upper: bool,
}

/// Evaluate symbol matching precedence:
/// 1. Block phrases strictly dominate every positive signal (`false`).
/// 2. Cashtags (`$SYM`) and explicit keywords match regardless of case/cashtag policy (`true`).
/// 3. If `require_cashtag` is set, bare tickers never match (`false`).
/// 4. Otherwise, bare tickers match according to `require_uppercase`.
pub const fn evaluate_symbol_match(policy: SymbolMatchPolicy, signals: SymbolMatchSignals) -> bool {
    if signals.has_block_phrase {
        return false;
    }
    if signals.has_cashtag || signals.has_keyword {
        return true;
    }
    if policy.require_cashtag {
        return false;
    }
    if policy.require_uppercase {
        signals.has_bare_ticker_upper
    } else {
        signals.has_bare_ticker_ci
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn block_phrase_dominates_cashtag_and_keyword() {
        let policy = SymbolMatchPolicy {
            require_cashtag: false,
            require_uppercase: false,
        };
        let signals = SymbolMatchSignals {
            has_block_phrase: true,
            has_cashtag: true,
            has_keyword: true,
            has_bare_ticker_ci: true,
            has_bare_ticker_upper: true,
        };
        assert!(!evaluate_symbol_match(policy, signals));
    }

    #[test]
    fn require_cashtag_rejects_bare_ticker() {
        let policy = SymbolMatchPolicy {
            require_cashtag: true,
            require_uppercase: false,
        };
        let signals = SymbolMatchSignals {
            has_block_phrase: false,
            has_cashtag: false,
            has_keyword: false,
            has_bare_ticker_ci: true,
            has_bare_ticker_upper: true,
        };
        assert!(!evaluate_symbol_match(policy, signals));
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    fn verify_symbol_match_precedence_invariants() {
        let policy = SymbolMatchPolicy {
            require_cashtag: kani::any(),
            require_uppercase: kani::any(),
        };
        let signals = SymbolMatchSignals {
            has_block_phrase: kani::any(),
            has_cashtag: kani::any(),
            has_keyword: kani::any(),
            has_bare_ticker_ci: kani::any(),
            has_bare_ticker_upper: kani::any(),
        };

        // Uppercase bare ticker match always implies case-insensitive match.
        kani::assume(!signals.has_bare_ticker_upper || signals.has_bare_ticker_ci);

        let matched = evaluate_symbol_match(policy, signals);

        // 1. Block phrases strictly dominate all positive signals.
        if signals.has_block_phrase {
            assert!(!matched);
        }

        // 2. Unblocked cashtags or keywords always match.
        if !signals.has_block_phrase && (signals.has_cashtag || signals.has_keyword) {
            assert!(matched);
        }

        // 3. `require_cashtag` blocks bare tickers when neither cashtag nor keyword matched.
        if policy.require_cashtag && !signals.has_cashtag && !signals.has_keyword {
            assert!(!matched);
        }

        // 4. `require_uppercase` blocks lowercase-only bare tickers.
        if policy.require_uppercase
            && !signals.has_cashtag
            && !signals.has_keyword
            && !signals.has_bare_ticker_upper
        {
            assert!(!matched);
        }

        // 5. Adding a positive cashtag (without adding a block phrase) never turns true -> false.
        let with_cashtag = SymbolMatchSignals {
            has_cashtag: true,
            ..signals
        };
        if matched {
            assert!(evaluate_symbol_match(policy, with_cashtag));
        }
    }
}
