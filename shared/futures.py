import pandas_market_calendars as mcal
from datetime import datetime, timedelta

# Per-symbol primary index future. Used for the chart overlay + the
# context-aware tile that shows up beside the selected equity.
PRIMARY_FUTURES_MAP = {
    "AMD": "NQ=F",
    "ASTS": "RTY=F",
    "INTC": "NQ=F",
    "MU": "NQ=F",
    "NVDA": "NQ=F",
    "RKLB": "RTY=F",
}

# Futures shown for all symbols (different signal type, not a price proxy).
GLOBAL_FUTURES = ["VX=F"]

# Map ticker -> market-calendar name. CME_Equity covers NQ/ES/RTY/YM.
# CFE covers VIX futures. Both have ~23h sessions with a daily break,
# but the exact break boundaries differ slightly.
FUTURES_CALENDAR_MAP = {
    "NQ=F": "CME_Equity",
    "RTY=F": "CME_Equity",
    "VX=F": "CFE",
}

# VIX stress thresholds
VIX_STRESS_LOW = 15.0
VIX_STRESS_HIGH = 25.0

# Initialize calendars once
_calendars = {
    "CME_Equity": mcal.get_calendar("CME_Equity"),
    "CFE": mcal.get_calendar("CFE"),
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
    return sorted(set(PRIMARY_FUTURES_MAP.values()) | set(GLOBAL_FUTURES))
