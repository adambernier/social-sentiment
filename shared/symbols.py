import re
import yaml
from pathlib import Path
import threading
import time
import json
import psycopg
import sys
import logging

import os
def get_env(key: str, default: str) -> str:
    return os.environ.get(key, default)
DATABASE_DSN = get_env("DATABASE_DSN", "postgresql://postgres:sentiment@localhost:5432/sentiment")

logger = logging.getLogger("shared-symbols")

# Initialize empty dict. fetch_symbols_from_db() will populate this on startup
SYMBOLS: dict[str, dict] = {}
SYMBOLS_LOCK = threading.Lock()

def fetch_symbols_from_db():
    try:
        with psycopg.connect(DATABASE_DSN) as conn:
            with conn.cursor() as cur:
                # We only fetch active symbols
                cur.execute("""
                    SELECT symbol, keywords, future, sector, require_uppercase, block_phrases 
                    FROM tracked_symbols 
                    WHERE is_active = true
                """)
                rows = cur.fetchall()
                new_symbols = {}
                for row in rows:
                    new_symbols[row[0]] = {
                        "keywords": row[1] if isinstance(row[1], list) else json.loads(row[1]),
                        "future": row[2],
                        "sector": row[3],
                        "require_uppercase": row[4],
                        "block_phrases": row[5] if isinstance(row[5], list) else json.loads(row[5]),
                    }
                
                if new_symbols:
                    global SYMBOLS
                    with SYMBOLS_LOCK:
                        SYMBOLS = new_symbols
    except Exception as e:
        logger.error(f"Failed to fetch symbols from DB: {e}")

def _symbol_refresher_loop():
    while True:
        time.sleep(60) # Poll every 60 seconds
        fetch_symbols_from_db()

# Start background thread to keep symbols fresh automatically
_refresher_thread = threading.Thread(target=_symbol_refresher_loop, daemon=True)
_refresher_thread.start()
# Do an initial synchronous fetch just in case the DB is ready
fetch_symbols_from_db()

def tickers() -> list[str]:
    with SYMBOLS_LOCK:
        return sorted(SYMBOLS.keys())

def keywords_map() -> dict[str, list[str]]:
    with SYMBOLS_LOCK:
        return {
            t: [t, f"${t}"] + cfg.get("keywords", [])
            for t, cfg in SYMBOLS.items()
        }

def primary_futures_map() -> dict[str, str]:
    with SYMBOLS_LOCK:
        return {t: cfg.get("future") for t, cfg in SYMBOLS.items()}

def sector_map() -> dict[str, str]:
    with SYMBOLS_LOCK:
        return {t: cfg.get("sector") for t, cfg in SYMBOLS.items()}

def match_symbol(text: str, symbol: str) -> bool:
    with SYMBOLS_LOCK:
        cfg = SYMBOLS.get(symbol)
    if not cfg:
        return False
    
    for phrase in cfg.get("block_phrases", []):
        if phrase.lower() in text.lower():
            return False
            
    cashtag = f"${symbol}"
    if re.search(rf"(?<![a-zA-Z0-9]){re.escape(cashtag)}(?![a-zA-Z0-9])", text, re.I):
        return True
        
    for kw in cfg.get("keywords", []):
        if re.search(rf"(?<![a-zA-Z0-9]){re.escape(kw)}(?![a-zA-Z0-9])", text, re.I):
            return True
            
    flags = 0 if cfg.get("require_uppercase") else re.I
    pattern = rf"(?<![a-zA-Z0-9]){re.escape(symbol)}(?![a-zA-Z0-9])"
    if re.search(pattern, text, flags):
        return True
        
    return False
