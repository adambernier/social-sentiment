import asyncio
import time
import threading
import sys

import pandas as pd
import pandas_market_calendars as mcal
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf
from yfinance.exceptions import YFRateLimitError

# Add root to sys.path for cross-service imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from shared.schemas import StockQuote, StockMetrics
from shared.config import get_env_int
from shared.futures import get_futures_session, all_polled_futures
from shared.symbols import tickers, sector_map
from shared.pacing import AsyncRateLimiter, PerSymbolBackoff, paced_gather
from storage_service.db import DB
from shared.metrics import start_metrics_server, POSTS_INGESTED_TOTAL, RATE_LIMITS_HIT_TOTAL
from synthetic_nav import SYNTHETIC_NAV_SYMBOLS, SyntheticNavRunner

# Initialize market calendar
nyse = mcal.get_calendar('NYSE')

SECTOR_MAP_CACHE = {}
POLL_INTERVAL = 60  # 1 minute
METRICS_INTERVAL = 3600 # 1 hour
# Request shaping for the per-symbol quote fan-out. Yahoo/yfinance is the most
# aggressive limiter we hit, so cap concurrency and pace request starts, and give
# each symbol its own backoff — mirrors the stocktwits/finnhub producers.
MARKET_MAX_CONCURRENCY = get_env_int("MARKET_MAX_CONCURRENCY", 6)
MARKET_RATE_PER_MIN = get_env_int("MARKET_RATE_PER_MIN", 120)
MARKET_MAX_BACKOFF = get_env_int("MARKET_MAX_BACKOFF", 3600)

def calculate_relative(value, baseline, invert=False):
    if value is None or baseline is None or baseline == 0:
        return 0.0
    # Simple relative score: (value - baseline) / baseline
    res = (value - baseline) / baseline
    return -res if invert else res

def fetch_and_store_metrics(db: DB):
    print("Updating financial metrics...")
    try:
        from defeatbeta_api.data.ticker import Ticker as DbTicker
    except ImportError:
        print("defeatbeta_api not installed. Run pip install defeatbeta-api duckdb")
        return

    sector_cache = {}
    
    current_symbols = tickers() + all_polled_futures()
    sector_mapping = sector_map()
    
    one_year_ago = pd.Timestamp.now() - pd.Timedelta(days=365)
    
    def get_db_metrics(symbol):
        def make_tz_naive(series):
            converted = pd.to_datetime(series)
            if converted.dt.tz is not None:
                return converted.dt.tz_convert(None).dt.normalize()
            return converted.dt.normalize()

        try:
            t = DbTicker(symbol)
            price_df = t.price()
            if price_df.empty:
                raise ValueError("Empty price data from defeatbeta-api")
            
            price_df['report_date'] = make_tz_naive(price_df['report_date'])
            hist = price_df[price_df['report_date'] >= one_year_ago].copy()
            if hist.empty:
                return None
            
            hist['return'] = hist['close'].pct_change()
            start_price = hist.iloc[0]['close']
            end_price = hist.iloc[-1]['close']
            avg_return = (end_price - start_price) / start_price
            
            # Fetch PE
            pe = None
            try:
                pe_df = t.ttm_pe()
                if not pe_df.empty:
                    pe = float(pe_df.iloc[-1]['ttm_pe'])
            except Exception:
                pass
                
            return {
                "hist": hist,
                "avg_return": avg_return,
                "pe": pe
            }
        except Exception as e:
            print(f"Error fetching data from defeatbeta-api for {symbol}: {e}. Trying yfinance fallback...")
            try:
                yf_ticker = yf.Ticker(symbol)
                yf_df = yf_ticker.history(period="1y")
                if yf_df.empty:
                    return None
                
                yf_df = yf_df.reset_index()
                yf_df['report_date'] = make_tz_naive(yf_df['Date'])
                yf_df = yf_df.rename(columns={
                    "Open": "open",
                    "Close": "close",
                    "High": "high",
                    "Low": "low",
                    "Volume": "volume"
                })
                yf_df['symbol'] = symbol
                
                hist = yf_df[['symbol', 'report_date', 'open', 'close', 'high', 'low', 'volume']].copy()
                hist['return'] = hist['close'].pct_change()
                
                start_price = hist.iloc[0]['close']
                end_price = hist.iloc[-1]['close']
                avg_return = (end_price - start_price) / start_price
                
                pe = None
                try:
                    pe = yf_ticker.info.get('trailingPE') or yf_ticker.info.get('forwardPE')
                    if pe:
                        pe = float(pe)
                except Exception:
                    pass
                    
                return {
                    "hist": hist,
                    "avg_return": avg_return,
                    "pe": pe
                }
            except Exception as yf_err:
                print(f"Failed to fetch data from yfinance fallback for {symbol}: {yf_err}")
                return None


    for symbol in current_symbols:
        try:
            # Skip non-equity instruments (futures, the ^VIX index) — defeatbeta
            # has no fundamentals (P/E, beta, ...) for them.
            if symbol.endswith("=F") or symbol.startswith("^"):
                continue

            metrics_data = get_db_metrics(symbol)
            if not metrics_data:
                continue
                
            hist = metrics_data["hist"]
            avg_return = metrics_data["avg_return"]
            pe = metrics_data["pe"]
            
            # Simple inflation adjustment (e.g., 3%)
            inflation_rate = 0.03
            inflation_adj_return = avg_return - inflation_rate
            
            # Fetch sector baseline (with local execution caching)
            sector_etf = sector_mapping.get(symbol, "SPY")
            if sector_etf not in sector_cache:
                print(f"Fetching sector baseline metrics for {sector_etf} via defeatbeta-api...")
                sector_cache[sector_etf] = get_db_metrics(sector_etf)
                
            s_data = sector_cache.get(sector_etf)
            if not s_data:
                print(f"Warning: No sector baseline data available for {sector_etf}, using defaults.")
                s_pe = None
                s_beta = 1.0
                s_return = 0.0
                beta = 1.0
            else:
                s_pe = s_data["pe"]
                s_return = s_data["avg_return"]
                # Sector Beta is 1.0 by definition relative to itself
                s_beta = 1.0
                
                # Calculate Beta manually (Covariance of stock vs sector)
                s_hist = s_data["hist"]
                
                # Align dates to compute covariance
                aligned = pd.merge(hist[['report_date', 'return']], s_hist[['report_date', 'return']], on='report_date', suffixes=('_stock', '_sector')).dropna()
                if len(aligned) > 30:
                    cov = aligned['return_stock'].cov(aligned['return_sector'])
                    var = aligned['return_sector'].var()
                    beta = cov / var if var != 0 else 1.0
                else:
                    beta = 1.0
            
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


def fetch_single_quote(symbol: str, now_utc: datetime) -> tuple[StockQuote | None, bool]:
    """Fetch the latest quote for one symbol.

    Returns ``(quote_or_None, was_rate_limited)``. The flag drives per-symbol
    backoff: only a genuine Yahoo rate-limit (``YFRateLimitError``) counts —
    empty data or other errors return ``(None, False)`` so a quiet or closed
    symbol isn't penalized as though it were throttled.
    """
    try:
        # Determine session based on instrument type
        if symbol.endswith("=F"):
            current_session = get_futures_session(symbol, now_utc)
        else:
            current_session = get_market_session(now_utc)

        # Skip fetching from yfinance if the market/futures session is closed or on break
        if current_session in ("closed", "futures_closed", "futures_break"):
            print(f"Skipping fetch for {symbol} (Session: {current_session} is closed/inactive)")
            return None, False

        print(f"Fetching {symbol} (Session: {current_session})...")
        ticker = yf.Ticker(symbol)
        # Fetch last 1 day of 1-minute data to get the absolute latest close
        data = ticker.history(period="1d", interval="1m")

        if data.empty:
            print(f"Warning: No data returned for {symbol}")
            # Treat empty data as potential rate limit/block to trigger backoff
            return None, True

        latest = data.iloc[-1]
        # Convert pandas Timestamp to UTC datetime
        ts = data.index[-1].to_pydatetime().astimezone(timezone.utc)

        try:
            daily_volume = int(ticker.fast_info.get("lastVolume") or latest["Volume"])
        except Exception:
            # fast_info is best-effort; we already have the price, so a hiccup
            # here isn't a rate-limit.
            daily_volume = int(latest["Volume"])

        return StockQuote(
            symbol=symbol,
            timestamp=ts,
            price=float(latest["Close"]),
            volume=daily_volume,
            market_session=current_session
        ), False
    except YFRateLimitError as e:
        print(f"RATE LIMITED by Yahoo fetching {symbol}: {e}")
        return None, True
    except Exception as e:
        print(f"ERROR fetching {symbol}: {e}")
        # Treat general exceptions (like 403 Forbidden, connection errors) as rate limit/block to trigger backoff
        return None, True


async def fetch_and_store(db: DB, limiter: AsyncRateLimiter, backoff_tracker: PerSymbolBackoff):
    now_utc = datetime.now(timezone.utc)

    # Synthetic-NAV symbols (mutual funds) have no 1m bars; an empty fetch reads
    # as a rate limit and would park them in permanent backoff, so they are
    # handled solely by SyntheticNavRunner.
    current_symbols = [s for s in tickers() if s not in SYNTHETIC_NAV_SYMBOLS] + all_polled_futures()
    # Skip symbols still in their own rate-limit cooldown so one throttled symbol
    # doesn't stall the rest. Each yfinance call is blocking, so run it in a worker
    # thread and shape the fan-out (concurrency cap + pacing) with the shared helper.
    due = backoff_tracker.due(current_symbols)
    results = await paced_gather(
        due,
        lambda sym: asyncio.to_thread(fetch_single_quote, sym, now_utc),
        max_concurrency=MARKET_MAX_CONCURRENCY,
        limiter=limiter,
    )

    # Write to database sequentially to prevent concurrent psycopg connection issues
    for sym, (quote, was_limited) in zip(due, results):
        backoff_tracker.record(sym, was_limited)
        if was_limited:
            RATE_LIMITS_HIT_TOTAL.labels(platform="market").inc()
            continue
        if quote is None:
            continue
        try:
            inserted = db.insert_quote(quote)
            if inserted:
                POSTS_INGESTED_TOTAL.labels(platform="market", symbol=quote.symbol).inc()
                print(f"SUCCESS: {quote.symbol} at {quote.timestamp.strftime('%H:%M:%S')} UTC -> ${quote.price:.2f} ({quote.market_session})")
            else:
                print(f"INFO: {quote.symbol} quote already exists for {quote.timestamp}")
        except Exception as e:
            print(f"ERROR saving {quote.symbol}: {e}")

    penalized = backoff_tracker.penalized()
    if penalized:
        print(
            f"{len(penalized)} symbol(s) in rate-limit backoff, "
            f"skipped {len(current_symbols) - len(due)} this cycle: {penalized}"
        )


def run_metrics_in_background():
    def worker():
        db_thread = None
        try:
            print("Starting background metrics update thread...")
            # The main market connection already applied the schema at startup.
            # This hourly secondary connection only needs an independent session;
            # reapplying all DDL here adds avoidable locks and table scans.
            db_thread = DB(apply_schema=False)
            fetch_and_store_metrics(db_thread)
            print("Background metrics update completed successfully.")
        except Exception as e:
            print(f"Error in background metrics update thread: {e}")
        finally:
            if db_thread is not None and db_thread.conn is not None:
                db_thread.conn.close()
            
    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()

async def main():
    try:
        db = DB()
        print("Connected to database.")
    except Exception as e:
        print(f"CRITICAL: Could not connect to database: {e}")
        return

    print(f"Market Producer started.")
    start_metrics_server(8003)
    print(f"Polling interval: {POLL_INTERVAL}s")

    limiter = AsyncRateLimiter(max_rate=MARKET_RATE_PER_MIN, period=60.0)
    backoff_tracker = PerSymbolBackoff(base_interval=POLL_INTERVAL, max_backoff=MARKET_MAX_BACKOFF)
    nav_runner = SyntheticNavRunner(db, limiter, backoff_tracker, nyse)
    last_metrics_update = 0.0

    while True:
        now = time.time()
        if now - last_metrics_update > METRICS_INTERVAL:
            run_metrics_in_background()
            last_metrics_update = now

        await fetch_and_store(db, limiter, backoff_tracker)
        now_utc = datetime.now(timezone.utc)
        await nav_runner.maybe_run(now_utc, get_market_session(now_utc))
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="market-producer")
