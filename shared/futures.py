import pandas_market_calendars as mcal
from datetime import datetime, timedelta

from shared.symbols import primary_futures_map
from shared.config import VIX_SYMBOL

# Market-wide volatility signal shown beside every symbol (different signal type,
# not a price proxy). Spot VIX (^VIX) is a cash-session index, so the market
# producer fetches it via the equity market session — not a futures session — and
# it isn't listed in FUTURES_CALENDAR_MAP below.
GLOBAL_FUTURES = [VIX_SYMBOL]

# Map ticker -> market-calendar name. CME_Equity covers NQ/ES/RTY/YM; these have
# ~23h sessions with a daily break.
FUTURES_CALENDAR_MAP = {
    "NQ=F": "CME_Equity",
    "RTY=F": "CME_Equity",
}

# VIX stress thresholds
VIX_STRESS_LOW = 15.0
VIX_STRESS_HIGH = 25.0

# Initialize calendars once
_calendars = {
    "CME_Equity": mcal.get_calendar("CME_Equity"),
}

def get_futures_session(symbol: str, now_utc: datetime) -> str:
    """
    Return the current futures session state for a given symbol: 
    'futures_open', 'futures_break', or 'futures_closed'.
    """
    cal_name = FUTURES_CALENDAR_MAP.get(symbol, "CME_Equity")
    cal = _calendars.get(cal_name)
    
    # Get schedule for a 3-day window to handle wraps and holidays
    schedule = cal.schedule(start_date=now_utc - timedelta(days=2), end_date=now_utc + timedelta(days=1))
    
    day_str = now_utc.strftime('%Y-%m-%d')
    
    if day_str not in schedule.index:
        return 'futures_closed'
    
    row = schedule.loc[day_str]
    m_open = row['market_open'].to_pydatetime()
    m_close = row['market_close'].to_pydatetime()
    
    if m_open <= now_utc <= m_close:
        return 'futures_open'
    
    # If it's a weekday and we aren't 'open' according to the span, it's a break
    if now_utc.weekday() < 5: # Mon-Fri
        return 'futures_break'
        
    return 'futures_closed'

def all_polled_futures() -> list[str]:
    """Unique futures tickers the market-producer should poll."""
    configured_futures = {
        future
        for future in primary_futures_map().values()
        if future is not None
    }
    return sorted(configured_futures | set(GLOBAL_FUTURES))
