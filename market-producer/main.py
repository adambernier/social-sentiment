import time
import threading
import sys

import pandas as pd
import pandas_market_calendars as mcal
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

# Add root to sys.path for cross-service imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from shared.schemas import StockQuote, StockMetrics
from shared.futures import get_futures_session, all_polled_futures
from shared.symbols import tickers, sector_map
from storage_service.db import DB

# Initialize market calendar
nyse = mcal.get_calendar('NYSE')

EQUITY_SYMBOLS = tickers()
SYMBOLS = EQUITY_SYMBOLS + all_polled_futures()
SECTOR_MAP = sector_map()
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
    sector_cache = {}
    
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
            
            # Fetch sector baseline (with local execution caching)
            sector_etf = SECTOR_MAP.get(symbol, "SPY")
            if sector_etf not in sector_cache:
                try:
                    print(f"Fetching sector baseline metrics for {sector_etf}...")
                    s_ticker = yf.Ticker(sector_etf)
                    s_info = s_ticker.info
                    s_hist = s_ticker.history(period="1y")
                    
                    if s_hist.empty:
                        sector_cache[sector_etf] = None
                    else:
                        s_start = s_hist.iloc[0]["Close"]
                        s_end = s_hist.iloc[-1]["Close"]
                        s_return = (s_end - s_start) / s_start
                        sector_cache[sector_etf] = {
                            "pe": s_info.get("trailingPE"),
                            "beta": s_info.get("beta") or 1.0,
                            "return": s_return
                        }
                except Exception as s_err:
                    print(f"Error fetching baseline sector {sector_etf}: {s_err}")
                    sector_cache[sector_etf] = None
            
            s_data = sector_cache.get(sector_etf)
            if not s_data:
                print(f"Warning: No sector baseline data available for {sector_etf}, using defaults.")
                s_pe = None
                s_beta = 1.0
                s_return = 0.0
            else:
                s_pe = s_data["pe"]
                s_beta = s_data["beta"]
                s_return = s_data["return"]
            
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


def get_market_session(now_utc: datetime) -> str:
    # Get schedule for current day (and tomorrow just in case of wrap-around)
    schedule = nyse.schedule(start_date=now_utc - timedelta(days=1), end_date=now_utc + timedelta(days=1))
    
    # Check if today is a trading day
    day_str = now_utc.strftime('%Y-%m-%d')
    if day_str not in schedule.index:
        return 'closed'
    
    row = schedule.loc[day_str]
    
    # Standard NYSE hours (UTC)
    # Pre-market: 4:00 AM - 9:30 AM ET
    # Regular: 9:30 AM - 4:00 PM ET
    # After-hours: 4:00 PM - 8:00 PM ET
    
    market_open = row['market_open'].to_pydatetime()
    market_close = row['market_close'].to_pydatetime()
    
    # Calculate pre and after offsets (standard NYSE rules)
    # yfinance and most data providers use these windows
    pre_market_open = market_open - timedelta(hours=5, minutes=30) # 4:00 AM ET
    after_hours_close = market_close + timedelta(hours=4)          # 8:00 PM ET

    if market_open <= now_utc <= market_close:
        return 'regular'
    elif pre_market_open <= now_utc < market_open:
        return 'pre'
    elif market_close < now_utc <= after_hours_close:
        return 'after'
    else:
        return 'closed'

def fetch_and_store(db: DB):
    now_utc = datetime.now(timezone.utc)
    
    for symbol in SYMBOLS:
        try:
            # Determine session based on instrument type
            if symbol.endswith("=F"):
                current_session = get_futures_session(symbol, now_utc)
            else:
                current_session = get_market_session(now_utc)
                
            print(f"Fetching {symbol} (Session: {current_session})...")
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
                volume=int(latest["Volume"]),
                market_session=current_session
            )
            
            inserted = db.insert_quote(quote)
            if inserted:
                print(f"SUCCESS: {symbol} at {ts.strftime('%H:%M:%S')} UTC -> ${quote.price:.2f} ({current_session})")
            else:
                print(f"INFO: {symbol} quote already exists for {ts}")
                
        except Exception as e:
            print(f"ERROR fetching {symbol}: {e}")

def run_metrics_in_background():
    def worker():
        try:
            print("Starting background metrics update thread...")
            db_thread = DB()
            fetch_and_store_metrics(db_thread)
            db_thread.conn.close()
            print("Background metrics update completed successfully.")
        except Exception as e:
            print(f"Error in background metrics update thread: {e}")
            
    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()

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
            run_metrics_in_background()
            last_metrics_update = now
            
        fetch_and_store(db)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
