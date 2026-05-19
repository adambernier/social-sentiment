"""Source of truth for per-symbol configuration.

Adding a new symbol is a one-line edit to SYMBOLS below; every producer
and the UI picks it up on next start.
"""

SYMBOLS: dict[str, dict] = {
    "AMD":  {"keywords": ["Advanced Micro Devices", "Lisa Su"], "future": "NQ=F",  "sector": "XLK"},
    "ASTS": {"keywords": ["SpaceMobile"],                       "future": "RTY=F", "sector": "XAR"},
    "INTC": {"keywords": ["Intel"],                             "future": "NQ=F",  "sector": "XLK"},
    "MU":   {"keywords": ["Micron"],                            "future": "NQ=F",  "sector": "XLK"},
    "NVDA": {"keywords": ["Nvidia", "Jensen Huang"],            "future": "NQ=F",  "sector": "XLK"},
    "RKLB": {"keywords": ["Rocket Lab"],                        "future": "RTY=F", "sector": "XAR"},
    "SMH":  {"keywords": ["VanEck Semiconductor"],              "future": "NQ=F",  "sector": "XLK"},
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
