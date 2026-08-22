use std::collections::HashMap;

use chrono::{DateTime, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::America::New_York;

use crate::{DailyClose, SessionCalendar};

pub const NAV_SYMBOL: &str = "SWYGX";
pub const MAX_PLAUSIBLE_MOVE: f64 = 8.0;
pub const HOLDINGS: [(&str, f64); 6] = [
    ("SCHB", 0.55),
    ("SCHF", 0.27),
    ("SCHE", 0.08),
    ("SCHZ", 0.06),
    ("SCHP", 0.02),
    ("SCHO", 0.02),
];

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
    let change = (latest.close - previous.close) / previous.close * 100.0;
    if !change.is_finite() || change.abs() > MAX_PLAUSIBLE_MOVE {
        anyhow::bail!("implausible proxy move");
    }
    Ok(change)
}

pub fn estimate_pct_change(
    histories: &HashMap<String, Vec<DailyClose>>,
    session_date: NaiveDate,
) -> Option<f64> {
    let mut weighted = 0.0;
    let mut covered = 0.0;
    for (symbol, weight) in HOLDINGS {
        let Ok(change) = histories
            .get(symbol)
            .ok_or_else(|| anyhow::anyhow!("missing proxy"))
            .and_then(|history| proxy_change(history, session_date))
        else {
            continue;
        };
        weighted += change * weight;
        covered += weight;
    }
    (covered > 0.0).then_some(weighted / covered)
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
