use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveTime, TimeZone, Utc, Weekday};
use chrono_tz::America::New_York;

use crate::SessionCalendar;

#[derive(Debug, Default, Clone, Copy)]
pub struct UsMarketCalendar;

impl UsMarketCalendar {
    fn observed(date: NaiveDate) -> NaiveDate {
        match date.weekday() {
            Weekday::Sat => date - Duration::days(1),
            Weekday::Sun => date + Duration::days(1),
            _ => date,
        }
    }

    fn nth_weekday(year: i32, month: u32, weekday: Weekday, nth: u32) -> NaiveDate {
        let first = NaiveDate::from_ymd_opt(year, month, 1).expect("valid month");
        let offset = (7 + weekday.num_days_from_monday() as i64
            - first.weekday().num_days_from_monday() as i64)
            % 7;
        first + Duration::days(offset + 7 * i64::from(nth - 1))
    }

    fn last_weekday(year: i32, month: u32, weekday: Weekday) -> NaiveDate {
        let next = if month == 12 {
            NaiveDate::from_ymd_opt(year + 1, 1, 1).unwrap()
        } else {
            NaiveDate::from_ymd_opt(year, month + 1, 1).unwrap()
        };
        let mut day = next - Duration::days(1);
        while day.weekday() != weekday {
            day -= Duration::days(1);
        }
        day
    }

    // Anonymous Gregorian algorithm; NYSE observes Good Friday.
    fn easter(year: i32) -> NaiveDate {
        let a = year % 19;
        let b = year / 100;
        let c = year % 100;
        let d = b / 4;
        let e = b % 4;
        let f = (b + 8) / 25;
        let g = (b - f + 1) / 3;
        let h = (19 * a + b - d - g + 15) % 30;
        let i = c / 4;
        let k = c % 4;
        let l = (32 + 2 * e + 2 * i - h - k) % 7;
        let m = (a + 11 * h + 22 * l) / 451;
        let month = (h + l - 7 * m + 114) / 31;
        let day = (h + l - 7 * m + 114) % 31 + 1;
        NaiveDate::from_ymd_opt(year, month as u32, day as u32).unwrap()
    }

    pub fn is_holiday(date: NaiveDate) -> bool {
        let year = date.year();
        let holidays = [
            Self::observed(NaiveDate::from_ymd_opt(year, 1, 1).unwrap()),
            Self::nth_weekday(year, 1, Weekday::Mon, 3),
            Self::nth_weekday(year, 2, Weekday::Mon, 3),
            Self::easter(year) - Duration::days(2),
            Self::last_weekday(year, 5, Weekday::Mon),
            Self::observed(NaiveDate::from_ymd_opt(year, 6, 19).unwrap()),
            Self::observed(NaiveDate::from_ymd_opt(year, 7, 4).unwrap()),
            Self::nth_weekday(year, 9, Weekday::Mon, 1),
            Self::nth_weekday(year, 11, Weekday::Thu, 4),
            Self::observed(NaiveDate::from_ymd_opt(year, 12, 25).unwrap()),
        ];
        holidays.contains(&date)
            || date == Self::observed(NaiveDate::from_ymd_opt(year + 1, 1, 1).unwrap())
    }

    pub fn is_early_close(date: NaiveDate) -> bool {
        let thanksgiving = Self::nth_weekday(date.year(), 11, Weekday::Thu, 4);
        let weekday = !matches!(date.weekday(), Weekday::Sat | Weekday::Sun);
        date == thanksgiving + Duration::days(1)
            || (date.month() == 7 && date.day() == 3 && weekday)
            || (date.month() == 12 && date.day() == 24 && weekday)
    }
}

impl SessionCalendar for UsMarketCalendar {
    fn equity_session(&self, now: DateTime<Utc>) -> String {
        let local = now.with_timezone(&New_York);
        if !self.is_trading_day(local.date_naive()) {
            return "closed".into();
        }
        let time = local.time();
        let pre = NaiveTime::from_hms_opt(4, 0, 0).unwrap();
        let open = NaiveTime::from_hms_opt(9, 30, 0).unwrap();
        let close = NaiveTime::from_hms_opt(
            if Self::is_early_close(local.date_naive()) {
                13
            } else {
                16
            },
            0,
            0,
        )
        .unwrap();
        let after = NaiveTime::from_hms_opt(20, 0, 0).unwrap();
        if time >= open && time <= close {
            "regular".into()
        } else if time >= pre && time < open {
            "pre".into()
        } else if time > close && time <= after {
            "after".into()
        } else {
            "closed".into()
        }
    }

    fn futures_session(&self, _symbol: &str, now: DateTime<Utc>) -> String {
        let local = now.with_timezone(&New_York);
        let time = local.time();
        let break_start = NaiveTime::from_hms_opt(17, 0, 0).unwrap();
        let break_end = NaiveTime::from_hms_opt(18, 0, 0).unwrap();
        if local.weekday() == Weekday::Sat
            || (local.weekday() == Weekday::Fri && time >= break_start)
            || (local.weekday() == Weekday::Sun && time < break_end)
        {
            "futures_closed".into()
        } else if time >= break_start && time < break_end {
            "futures_break".into()
        } else {
            "futures_open".into()
        }
    }

    fn is_trading_day(&self, date: NaiveDate) -> bool {
        !matches!(date.weekday(), Weekday::Sat | Weekday::Sun) && !Self::is_holiday(date)
    }

    fn previous_trading_day(&self, date: NaiveDate) -> Option<NaiveDate> {
        (1..=10)
            .map(|days| date - Duration::days(days))
            .find(|candidate| self.is_trading_day(*candidate))
    }
}

pub fn local_datetime_to_utc(
    date: NaiveDate,
    time: NaiveTime,
    timezone: chrono_tz::Tz,
) -> anyhow::Result<DateTime<Utc>> {
    timezone
        .from_local_datetime(&date.and_time(time))
        .single()
        .map(|value| value.with_timezone(&Utc))
        .ok_or_else(|| anyhow::anyhow!("ambiguous or nonexistent local session time"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn holiday_early_close_and_dst_sessions() {
        let calendar = UsMarketCalendar;
        let july_fourth = Utc.with_ymd_and_hms(2026, 7, 3, 16, 0, 0).unwrap();
        assert_eq!(calendar.equity_session(july_fourth), "closed");
        let summer_open = Utc.with_ymd_and_hms(2026, 8, 21, 13, 30, 0).unwrap();
        let winter_open = Utc.with_ymd_and_hms(2026, 12, 21, 14, 30, 0).unwrap();
        assert_eq!(calendar.equity_session(summer_open), "regular");
        assert_eq!(calendar.equity_session(winter_open), "regular");
    }
}
