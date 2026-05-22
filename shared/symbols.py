import re

"""Source of truth for per-symbol configuration.

Adding a new symbol is a one-line edit to SYMBOLS below; every producer
and the UI picks it up on next start.
"""

SYMBOLS: dict[str, dict] = {
    "AMD":  {"keywords": ["Advanced Micro Devices", "Lisa Su"], "future": "NQ=F",  "sector": "XLK"},
    "ASTS": {"keywords": ["SpaceMobile"],                       "future": "RTY=F", "sector": "XAR"},
    "CRWV": {"keywords": ["CoreWeave"],                         "future": "NQ=F",  "sector": "XLK"},
    "INTC": {"keywords": ["Intel"],                             "future": "NQ=F",  "sector": "XLK"},
    "MU":   {
        "keywords": ["Micron"],
        "future": "NQ=F", "sector": "XLK",
        "require_uppercase": True,
        "block_phrases": ["mu wave", "mu metal", "mu meson", "mu-wave", "mu-metal"]
    },
    "NVDA": {"keywords": ["Nvidia", "Jensen Huang"],            "future": "NQ=F",  "sector": "XLK"},
    "RKLB": {"keywords": ["Rocket Lab"],                        "future": "RTY=F", "sector": "XAR"},
    "SMCI": {"keywords": ["Super Micro Computer", "Supermicro", "Charles Liang"], "future": "NQ=F",  "sector": "XLK"},
    "SMH":  {
        "keywords": ["VanEck Semiconductor"],
        "future": "NQ=F", "sector": "XLK",
        "require_uppercase": True,
        "block_phrases": ["shaking my head", "smh tbh", "smh at", "/s smh", "smfh"]
    },
}


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
