import re

import yaml
from pathlib import Path

"""Source of truth for per-symbol configuration.

Loads the tracker configuration dynamically from symbols.yaml at the project root.
"""

def load_symbols() -> dict[str, dict]:
    # Project root is two levels up from shared/symbols.py
    # (i.e. /shared/symbols.py -> /)
    config_path = Path(__file__).resolve().parent.parent / "symbols.yaml"
    if not config_path.exists():
        # Fallback for testing or if missing
        return {}
        
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
        
    return config if config else {}

SYMBOLS: dict[str, dict] = load_symbols()


def tickers() -> list[str]:
    return sorted(SYMBOLS.keys())


def keywords_map() -> dict[str, list[str]]:
    # Auto-prepends ticker and $cashtag — assumes cashtag is always $<TICKER>,
    # which holds for US equities and ETFs but would need adjustment for
    # tickers containing dots or dashes.
    return {
        t: [t, f"${t}"] + cfg["keywords"]
        for t, cfg in SYMBOLS.items()
    }


def primary_futures_map() -> dict[str, str]:
    return {t: cfg["future"] for t, cfg in SYMBOLS.items()}


def sector_map() -> dict[str, str]:
    return {t: cfg["sector"] for t, cfg in SYMBOLS.items()}


def match_symbol(text: str, symbol: str) -> bool:
    """
    Returns True if the text contains a high-precision match for the symbol.
    Applies case-sensitivity and block-phrase filtering for ambiguous tickers.
    """
    cfg = SYMBOLS.get(symbol)
    if not cfg:
        return False
    
    # 1. Global Block Phrases (Case-Insensitive)
    # If any block phrase is found anywhere in the text, it's a reject.
    for phrase in cfg.get("block_phrases", []):
        if phrase.lower() in text.lower():
            return False
            
    # 2. Cashtag Match (Highest Precision, Case-Insensitive)
    # $NVDA, $nvda, $MU, $mu are all high-signal.
    cashtag = f"${symbol}"
    if re.search(rf"(?<![a-zA-Z0-9]){re.escape(cashtag)}(?![a-zA-Z0-9])", text, re.I):
        return True
        
    # 3. Company Keywords Match (High Precision, Case-Insensitive)
    # "Nvidia", "Micron", "SpaceMobile" are high-signal regardless of case.
    for kw in cfg.get("keywords", []):
        if re.search(rf"(?<![a-zA-Z0-9]){re.escape(kw)}(?![a-zA-Z0-9])", text, re.I):
            return True
            
    # 4. Bare Ticker Match
    # If require_uppercase is set, we strictly match uppercase 'SYMBOL' (e.g. SMH)
    # Otherwise, we match case-insensitively (e.g. asts).
    flags = 0 if cfg.get("require_uppercase") else re.I
    pattern = rf"(?<![a-zA-Z0-9]){re.escape(symbol)}(?![a-zA-Z0-9])"
    if re.search(pattern, text, flags):
        return True
        
    return False
