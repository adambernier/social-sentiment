use std::collections::HashMap;

use chrono::{DateTime, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::America::New_York;

use crate::{DailyClose, SessionCalendar};

pub const NAV_SYMBOL: &str = "SWYGX";
pub const MAX_PLAUSIBLE_MOVE: f64 = social_sentiment_math::market::MAX_PLAUSIBLE_MOVE;
pub const HOLDINGS: [(&str, f64); 6] = social_sentiment_math::market::NAV_HOLDINGS;

pub fn proxy_change(history: &[DailyClose], session_date: NaiveDate) -> anyhow::Result<f64> {
    let latest = history
        .last()
        .ok_or_else(|| anyhow::anyhow!("no price data"))?;
    if latest.date != session_date {
        anyhow::bail!("no bar for session date");
    }
    let previous = history
        .iter()
        .rev()
        .find(|close| close.date < session_date)
        .ok_or_else(|| anyhow::anyhow!("insufficient price history"))?;
    social_sentiment_math::market::proxy_change_pct(previous.close, latest.close)
        .map_err(|err| anyhow::anyhow!("{err}"))
}

pub fn estimate_pct_change(
    histories: &HashMap<String, Vec<DailyClose>>,
    session_date: NaiveDate,
) -> Option<f64> {
    let mut changes = [None; 6];
    for (slot, (symbol, _)) in changes.iter_mut().zip(HOLDINGS.iter()) {
        *slot = histories
            .get(*symbol)
            .and_then(|history| proxy_change(history, session_date).ok());
    }
    social_sentiment_math::market::weighted_proxy_return(changes)
}

pub fn official_is_final(date: NaiveDate, now_utc: DateTime<Utc>) -> bool {
    let local = now_utc.with_timezone(&New_York);
    date < local.date_naive()
        || (date == local.date_naive()
            && local.time() >= NaiveTime::from_hms_opt(18, 15, 0).unwrap())
}

pub fn official_close_timestamp(date: NaiveDate) -> DateTime<Utc> {
    New_York
        .from_local_datetime(&date.and_hms_opt(16, 0, 0).unwrap())
        .single()
        .expect("NYSE close is unambiguous")
        .with_timezone(&Utc)
}

pub fn valid_baseline(
    navs: &[DailyClose],
    session_date: NaiveDate,
    calendar: &dyn SessionCalendar,
) -> Option<DailyClose> {
    let expected = calendar.previous_trading_day(session_date)?;
    navs.iter()
        .rev()
        .find(|close| close.date == expected)
        .cloned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sessions::UsMarketCalendar;
    use chrono::TimeZone;

    #[test]
    fn estimates_renormalize_available_proxies() {
        let date = NaiveDate::from_ymd_opt(2026, 8, 21).unwrap();
        let mut histories = HashMap::new();
        histories.insert(
            "SCHB".into(),
            vec![
                DailyClose {
                    date: date.pred_opt().unwrap(),
                    close: 100.0,
                },
                DailyClose { date, close: 102.0 },
            ],
        );
        assert_eq!(estimate_pct_change(&histories, date), Some(2.0));
    }

    #[test]
    fn official_publication_window_is_dst_safe() {
        let date = NaiveDate::from_ymd_opt(2026, 8, 21).unwrap();
        assert!(!official_is_final(
            date,
            Utc.with_ymd_and_hms(2026, 8, 21, 22, 0, 0).unwrap()
        ));
        assert!(official_is_final(
            date,
            Utc.with_ymd_and_hms(2026, 8, 21, 22, 15, 0).unwrap()
        ));
        assert_eq!(valid_baseline(&[], date, &UsMarketCalendar), None);
    }
}
