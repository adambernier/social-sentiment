import time
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

# Add root to sys.path for cross-service imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from shared.schemas import StockQuote, StockMetrics
from storage_service.db import DB

SYMBOLS = ["ASTS", "RKLB", "INTC", "NVDA"]
# Sector proxies (using ETFs)
SECTOR_MAP = {
    "ASTS": "XAR",  # Space/Tech
    "RKLB": "XAR",
    "INTC": "XLK",  # Semiconductors/Tech
    "NVDA": "XLK",
}
POLL_INTERVAL = 60  # 1 minute
METRICS_INTERVAL = 3600 # 1 hour

def calculate_relative(value, baseline, invert=False):
    if value is None or baseline is None or baseline == 0:
        return 0.0
    # Simple relative score: (value - baseline) / baseline
    res = (value - baseline) / baseline
    return -res if invert else res

def fetch_and_store_metrics(db: DB):
    print("Updating financial metrics...")
    for symbol in SYMBOLS:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            # Fetch 1y history for returns
            hist = ticker.history(period="1y")
            if hist.empty:
                continue
            
            start_price = hist.iloc[0]["Close"]
            end_price = hist.iloc[-1]["Close"]
            avg_return = (end_price - start_price) / start_price
            
            # Simple inflation adjustment (e.g., 3%)
            inflation_rate = 0.03
            inflation_adj_return = avg_return - inflation_rate
            
            pe = info.get("trailingPE")
            beta = info.get("beta")
            
            # Fetch sector baseline
            sector_etf = SECTOR_MAP.get(symbol, "SPY")
            s_ticker = yf.Ticker(sector_etf)
            s_info = s_ticker.info
            s_hist = s_ticker.history(period="1y")
            
            s_pe = s_info.get("trailingPE")
            # If sector beta is missing, use 1.0 as a sane baseline for market beta
            s_beta = s_info.get("beta") or 1.0
            
            s_start = s_hist.iloc[0]["Close"]
            s_end = s_hist.iloc[-1]["Close"]
            s_return = (s_end - s_start) / s_start
            
            # Relative scores
            # For P/E, lower is better, so invert=True
            pe_rel = calculate_relative(pe, s_pe, invert=True) if pe and s_pe else 0.0
            # For Beta, lower is "less risky", so invert=True to penalize high volatility
            beta_rel = calculate_relative(beta, s_beta, invert=True) if beta and s_beta else 0.0
            ret_rel = calculate_relative(avg_return, s_return) if avg_return and s_return else 0.0
            
            metrics = StockMetrics(
                symbol=symbol,
                pe_ratio=pe,
                beta=beta,
                avg_return_1y=avg_return,
                inflation_adj_return_1y=inflation_adj_return,
                pe_relative_sector=pe_rel,
                beta_relative_sector=beta_rel,
                return_relative_sector=ret_rel
            )
            
            db.upsert_metrics(metrics)
            print(f"Updated metrics for {symbol}")
            
        except Exception as e:
            print(f"Error updating metrics for {symbol}: {e}")

def fetch_and_store(db: DB):
    for symbol in SYMBOLS:
        try:
            print(f"Fetching {symbol}...")
            ticker = yf.Ticker(symbol)
            # Fetch last 1 day of 1-minute data to get the absolute latest close
            data = ticker.history(period="1d", interval="1m")
            
            if data.empty:
                print(f"Warning: No data returned for {symbol}")
                continue
            
            latest = data.iloc[-1]
            # Convert pandas Timestamp to UTC datetime
            ts = data.index[-1].to_pydatetime().astimezone(timezone.utc)
            
            quote = StockQuote(
                symbol=symbol,
                timestamp=ts,
                price=float(latest["Close"]),
                volume=int(latest["Volume"])
            )
            
            inserted = db.insert_quote(quote)
            if inserted:
                print(f"SUCCESS: {symbol} at {ts.strftime('%H:%M:%S')} UTC -> ${quote.price:.2f}")
            else:
                print(f"INFO: {symbol} quote already exists for {ts}")
                
        except Exception as e:
            print(f"ERROR fetching {symbol}: {e}")

def main():
    try:
        db = DB()
        print("Connected to database.")
    except Exception as e:
        print(f"CRITICAL: Could not connect to database: {e}")
        return

    print(f"Market Producer started. Tracking: {SYMBOLS}")
    print(f"Polling interval: {POLL_INTERVAL}s")
    
    last_metrics_update = 0
    
    while True:
        now = time.time()
        if now - last_metrics_update > METRICS_INTERVAL:
            fetch_and_store_metrics(db)
            last_metrics_update = now
            
        fetch_and_store(db)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
