"""Simulated intraday NAV for SWYGX, ported from the standalone realtime-nav script.

SWYGX (Schwab Target 2060 Index Fund) is a fund-of-funds holding Schwab index
*mutual funds*, which only price once daily (Yahoo publishes the NAV ~6pm ET).
Its intraday move is approximated as a weighted average of the daily % moves of
liquid ETF proxies tracking the same indexes, renormalized over the proxies
that could actually be priced. Estimates are written to stock_quotes as
market_session='regular' rows (volume=0) so the dashboard treats them like any
live price; the official NAV, once published, is inserted backdated to that
session's 16:00 ET close.

A 'regular' row must never appear on a non-trading day: the leaderboard derives
its trading-day calendar from regular-session rows across all symbols. The
estimate path is gated on the live session being 'regular' and the official-NAV
path on the NYSE schedule, so neither can write outside a trading day.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from shared.config import get_env_int
from shared.schemas import StockQuote
from shared.metrics import POSTS_INGESTED_TOTAL, RATE_LIMITS_HIT_TOTAL

ET = ZoneInfo("America/New_York")

NAV_SYMBOL = "SWYGX"
# Symbols handled here instead of the per-symbol 1m quote fan-out (mutual funds
# have no 1m bars; an empty fetch reads as a rate limit and triggers backoff).
SYNTHETIC_NAV_SYMBOLS = frozenset({NAV_SYMBOL})

# Weights must sum to 1.0. Verify against the latest Schwab SWYGX fact sheet —
# the target-date glide path shifts these over time.
HOLDINGS: dict[str, tuple[str, float]] = {
    # proxy_ticker: (underlying mutual fund, weight)
    "SCHB": ("SWTSX  US Total Market",      0.55),
    "SCHF": ("SWISX  Intl Developed",       0.27),
    "SCHE": ("SFENX  Emerging Markets",     0.08),
    "SCHZ": ("SWAGX  US Agg Bond",          0.06),
    "SCHP": ("SWRSX  TIPS",                 0.02),
    "SCHO": ("SWSBX  Short-Term Treasury",  0.02),
}

# Per-instrument daily moves beyond this magnitude (percent) are treated as bad
# data -- an unadjusted split, a wrong-scale tick, a corrupt baseline -- and
# skipped rather than trusted. Broad index funds essentially never move this
# much in a day.
MAX_PLAUSIBLE_MOVE = 8.0

# How often the synthetic path calls Yahoo (2 batched requests per cycle
# in-session). The estimate tracks broad indexes, so a faster cadence buys
# nothing while eating into the shared rate budget.
SYNTHETIC_NAV_INTERVAL = get_env_int("SYNTHETIC_NAV_INTERVAL", 300)

# Yahoo can show a today-dated daily row intraday with a stale/repeated value;
# only trust a close dated today after the NAV has actually been published.
OFFICIAL_NAV_EARLIEST_ET = dtime(18, 15)


def fetch_proxy_closes() -> pd.DataFrame:
    """Daily closes for every proxy, fetched in a single batched request.

    One call replaces a per-ticker fan-out, which is faster and far less
    likely to trip Yahoo's rate limiter. auto_adjust=False keeps closes
    unadjusted so prev and last share the same basis; an ex-dividend day is
    then a real price move, not a phantom one.
    """
    import yfinance as yf

    data = yf.download(
        list(HOLDINGS),
        period="5d",
        interval="1d",
        auto_adjust=False,
        group_by="column",
        progress=False,
    )
    if data is None or data.empty or "Close" not in data:
        raise RuntimeError("no proxy price data returned from Yahoo")
    return data["Close"]


def fetch_official_navs() -> pd.Series:
    """Recent daily closes (published NAVs) for the fund itself.

    auto_adjust=False is required: Ticker.history() defaults to True, which
    rewrites closes on distribution dates — we need the NAV as published.
    """
    import yfinance as yf

    hist = yf.Ticker(NAV_SYMBOL).history(period="5d", interval="1d", auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist:
        raise RuntimeError("no NAV data returned from Yahoo")
    return hist["Close"].dropna()


def proxy_change(closes: pd.Series, session_date: date) -> float:
    """% move for one proxy from the prior session's close to today's bar.

    prev and last come from the same series, so they can never be mismatched.
    Raises on missing history, a missing bar for session_date (a stale last
    close must not read as a 0% move), or an implausibly large move.
    """
    s = closes.dropna()
    if s.empty:
        raise RuntimeError("no price data")
    if s.index[-1].date() != session_date:
        raise RuntimeError(f"no bar for {session_date} yet (latest {s.index[-1].date()})")
    prior = s[s.index.date < session_date]
    if prior.empty:
        raise RuntimeError("insufficient price history")
    last = float(s.iloc[-1])
    prev = float(prior.iloc[-1])
    pct = (last - prev) / prev * 100
    if abs(pct) > MAX_PLAUSIBLE_MOVE:
        raise RuntimeError(f"implausible move {pct:+.1f}% (likely bad data)")
    return pct


def estimate_pct_change(closes: pd.DataFrame, session_date: date) -> float | None:
    """Weighted NAV % change estimate, renormalized over priced proxies.

    A proxy that can't be priced is skipped rather than treated as a 0% move,
    so the estimate reflects only the holdings we could actually observe.
    Returns None when nothing could be priced.
    """
    weighted = 0.0
    covered = 0.0  # sum of weights we could actually price
    for ticker, (label, weight) in HOLDINGS.items():
        try:
            series = closes[ticker] if ticker in closes else pd.Series(dtype=float)
            pct = proxy_change(series, session_date)
        except Exception as e:  # one bad ticker must not poison the total
            print(f"SYNTH {NAV_SYMBOL}: skipping proxy {ticker} ({label.strip()}): {e}")
            continue
        weighted += pct * weight
        covered += weight
    if covered == 0.0:
        return None
    return weighted / covered


def is_final_close(d: date, now_et: datetime) -> bool:
    """Whether a daily close dated ``d`` can be trusted as a published NAV."""
    if d < now_et.date():
        return True
    if d == now_et.date():
        return now_et.time() >= OFFICIAL_NAV_EARLIEST_ET
    return False


def official_close_timestamp(session_date: date) -> datetime:
    """UTC timestamp of that session's 16:00 ET close (DST-safe)."""
    return datetime.combine(session_date, dtime(16, 0), tzinfo=ET).astimezone(timezone.utc)


def trading_days(calendar, start: date, end: date) -> set[date]:
    schedule = calendar.schedule(start_date=start, end_date=end)
    return {ts.date() for ts in schedule.index}


def previous_trading_date(calendar, session_date: date) -> date | None:
    prior = sorted(
        d for d in trading_days(calendar, session_date - timedelta(days=10), session_date)
        if d < session_date
    )
    return prior[-1] if prior else None


class SyntheticNavRunner:
    """Drives synthetic SWYGX quote inserts from the market-producer poll loop.

    Call :meth:`maybe_run` every poll cycle; it self-throttles to
    ``SYNTHETIC_NAV_INTERVAL`` and only calls Yahoo when either an intraday
    estimate is due (live session is 'regular') or the official NAV is due
    (trading day, past the publication window, not yet stored) — zero calls
    overnight, on weekends, and on holidays.
    """

    def __init__(self, db, limiter, backoff, calendar, interval: float = SYNTHETIC_NAV_INTERVAL):
        self.db = db
        self.limiter = limiter
        self.backoff = backoff
        self.calendar = calendar
        self.interval = float(interval)
        self._next_run = 0.0
        self._official_done_for: date | None = None
        total_weight = sum(w for _, w in HOLDINGS.values())
        if abs(total_weight - 1.0) > 0.001:
            print(f"SYNTH {NAV_SYMBOL}: warning: weights sum to {total_weight:.3f}, not 1.0")

    async def maybe_run(self, now_utc: datetime, session: str) -> None:
        if time.monotonic() < self._next_run:
            return
        self._next_run = time.monotonic() + self.interval
        if not self.backoff.due([NAV_SYMBOL]):
            return

        now_et = now_utc.astimezone(ET)
        today_et = now_et.date()
        estimate_due = session == "regular"
        official_due = (
            self._official_done_for != today_et
            and now_et.time() >= OFFICIAL_NAV_EARLIEST_ET
            and today_et in trading_days(self.calendar, today_et, today_et)
        )
        if not estimate_due and not official_due:
            return

        try:
            navs = await self._fetch(fetch_official_navs)
        except Exception as e:
            self._record_failure(f"fetching official NAVs failed: {e}")
            return

        self._upsert_official_rows(navs, now_et)

        if estimate_due:
            baseline = self._baseline_from(navs, today_et)
            if baseline is not None:
                b_date, b_nav = baseline
                try:
                    proxies = await self._fetch(fetch_proxy_closes)
                except Exception as e:
                    self._record_failure(f"fetching proxy closes failed: {e}")
                    return
                pct = estimate_pct_change(proxies, today_et)
                if pct is None:
                    print(f"SYNTH {NAV_SYMBOL}: no proxies could be priced; skipping estimate")
                else:
                    self._insert_estimate(b_date, b_nav, pct, now_utc, today_et)

        self.backoff.record(NAV_SYMBOL, False)

    async def _fetch(self, fn):
        # yfinance is blocking; pace against the shared Yahoo budget, then run
        # in a worker thread like the main quote fan-out does.
        await self.limiter.acquire()
        return await asyncio.to_thread(fn)

    def _record_failure(self, what: str) -> None:
        self.backoff.record(NAV_SYMBOL, True)
        RATE_LIMITS_HIT_TOTAL.labels(platform="market").inc()
        print(f"SYNTH {NAV_SYMBOL}: {what}; backing off")

    def _upsert_official_rows(self, navs: pd.Series, now_et: datetime) -> None:
        """Insert published NAVs backdated to their session's 16:00 ET close.

        Idempotent via the (symbol, timestamp) ON CONFLICT no-op; the 5d window
        backfills official rows after downtime. A close is skipped when its
        date isn't an NYSE trading day, when it isn't final yet
        (:func:`is_final_close`), when the day-over-day move is implausible, or
        when a today-dated value exactly repeats the prior close (likely a
        stale row — a genuinely unchanged NAV self-heals tomorrow, when the
        date being in the past makes it trusted unconditionally).
        """
        today_et = now_et.date()
        dates = [ts.date() for ts in navs.index]
        if not dates:
            return
        tdays = trading_days(self.calendar, min(dates), max(dates))
        prev_close: float | None = None
        for ts, close in navs.items():
            d = ts.date()
            close = float(close)
            if d not in tdays or not is_final_close(d, now_et):
                continue
            if prev_close is not None:
                move = abs(close - prev_close) / prev_close * 100
                if move > MAX_PLAUSIBLE_MOVE:
                    print(f"SYNTH {NAV_SYMBOL}: skipping NAV for {d}: implausible move {move:.1f}%")
                    continue
                if d == today_et and close == prev_close:
                    print(f"SYNTH {NAV_SYMBOL}: NAV for {d} repeats prior close exactly; deferring as possibly stale")
                    continue
            quote = StockQuote(
                symbol=NAV_SYMBOL,
                timestamp=official_close_timestamp(d),
                # yfinance serves float32; round away the representation noise
                price=round(close, 4),
                volume=0,
                market_session="regular",
            )
            try:
                inserted = self.db.insert_quote(quote)
            except Exception as e:
                print(f"ERROR saving {NAV_SYMBOL} official NAV for {d}: {e}")
                continue
            prev_close = close
            if inserted:
                POSTS_INGESTED_TOTAL.labels(platform="market", symbol=NAV_SYMBOL).inc()
                print(f"SYNTH {NAV_SYMBOL}: official NAV {d} -> ${close:.4f}")
            if d == today_et:
                self._official_done_for = today_et

    def _baseline_from(self, navs: pd.Series, today_et: date) -> tuple[date, float] | None:
        """Latest prior-day published NAV, required to be the previous trading day.

        A 1-day proxy move must never be compounded onto an older baseline —
        the result would silently drop the missing days' moves.
        """
        prior = [(ts.date(), float(v)) for ts, v in navs.items() if ts.date() < today_et]
        if not prior:
            print(f"SYNTH {NAV_SYMBOL}: no prior NAV in window; skipping estimate")
            return None
        b_date, b_nav = prior[-1]
        expected = previous_trading_date(self.calendar, today_et)
        if expected is None or b_date != expected:
            print(f"SYNTH {NAV_SYMBOL}: baseline NAV dated {b_date}, expected {expected}; skipping estimate")
            return None
        return b_date, b_nav

    def _insert_estimate(self, b_date: date, b_nav: float, pct: float,
                         now_utc: datetime, today_et: date) -> None:
        est_ts = now_utc.replace(second=0, microsecond=0)
        if est_ts >= official_close_timestamp(today_et):
            # An estimate stamped at/after 16:00 ET would win the ON CONFLICT
            # race against the official close row and block it permanently.
            return
        price = round(b_nav * (1 + pct / 100), 4)
        quote = StockQuote(
            symbol=NAV_SYMBOL,
            timestamp=est_ts,
            price=price,
            volume=0,
            market_session="regular",
        )
        try:
            inserted = self.db.insert_quote(quote)
        except Exception as e:
            print(f"ERROR saving {NAV_SYMBOL} estimate: {e}")
            return
        if inserted:
            POSTS_INGESTED_TOTAL.labels(platform="market", symbol=NAV_SYMBOL).inc()
            print(f"SYNTH {NAV_SYMBOL}: est {pct:+.2f}% -> ${price:.4f} (baseline {b_date} ${b_nav:.4f})")
