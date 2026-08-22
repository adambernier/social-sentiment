use std::time::Duration;

use anyhow::{Context, Result};
use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, NaiveTime, TimeDelta, TimeZone, Utc};
use chrono_tz::Tz;
use reqwest::{Client, Response, StatusCode};
use serde_json::Value;
use social_sentiment_core::{
    producer::{AdapterOutcome, ProviderStatus},
    repository::GlobalMarketBar,
    schemas::{StockMetrics, StockQuote},
};
use url::Url;

use crate::{
    calculations::{build_metrics, normalize_fx_ohlc},
    DailyClose, FundamentalsProvider, MarketInstrument, QuoteProvider,
};

#[derive(Debug, Clone, PartialEq)]
pub struct ChartBar {
    pub timestamp: DateTime<Utc>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: Option<f64>,
}

#[derive(Clone)]
pub struct YahooProvider {
    client: Client,
    chart_base: String,
    summary_base: String,
}

impl YahooProvider {
    pub fn new(client: Client) -> Self {
        Self {
            client,
            chart_base: "https://query1.finance.yahoo.com/v8/finance/chart".into(),
            summary_base: "https://query1.finance.yahoo.com/v10/finance/quoteSummary".into(),
        }
    }

    pub fn with_bases(client: Client, chart_base: String, summary_base: String) -> Self {
        Self {
            client,
            chart_base,
            summary_base,
        }
    }

    async fn chart(
        &self,
        symbol: &str,
        interval: &str,
        period1: Option<i64>,
        period2: Option<i64>,
        range: Option<&str>,
    ) -> Result<AdapterOutcome<Value>> {
        let mut url = Url::parse(&self.chart_base)?;
        url.path_segments_mut()
            .map_err(|_| anyhow::anyhow!("invalid Yahoo chart base"))?
            .push(symbol);
        let mut request = self.client.get(url).query(&[
            ("interval", interval),
            ("events", "history"),
            ("includePrePost", "true"),
        ]);
        if let Some(range) = range {
            request = request.query(&[("range", range)]);
        }
        if let (Some(start), Some(end)) = (period1, period2) {
            request = request.query(&[("period1", start), ("period2", end)]);
        }
        parse_http_json(request.send().await?).await
    }

    async fn trailing_pe(&self, symbol: &str) -> Result<Option<f64>> {
        let mut url = Url::parse(&self.summary_base)?;
        url.path_segments_mut()
            .map_err(|_| anyhow::anyhow!("invalid Yahoo summary base"))?
            .push(symbol);
        let outcome = parse_http_json(
            self.client
                .get(url)
                .query(&[("modules", "summaryDetail,defaultKeyStatistics")])
                .send()
                .await?,
        )
        .await?;
        if outcome.status != ProviderStatus::Success {
            return Ok(None);
        }
        let value = &outcome.items[0];
        Ok(value
            .pointer("/quoteSummary/result/0/summaryDetail/trailingPE/raw")
            .or_else(|| value.pointer("/quoteSummary/result/0/summaryDetail/forwardPE/raw"))
            .and_then(Value::as_f64)
            .filter(|value| value.is_finite()))
    }

    async fn daily_history(&self, symbol: &str) -> Result<AdapterOutcome<DailyClose>> {
        let outcome = self.chart(symbol, "1d", None, None, Some("1y")).await?;
        if !matches!(
            outcome.status,
            ProviderStatus::Success | ProviderStatus::NoData
        ) {
            return Ok(AdapterOutcome::failure(outcome.status, outcome.retry_after));
        }
        Ok(AdapterOutcome::success(
            parse_chart(&outcome.items[0])?
                .into_iter()
                .map(|bar| DailyClose {
                    date: bar.timestamp.date_naive(),
                    close: bar.close,
                })
                .collect(),
        ))
    }

    pub async fn fetch_global_bars(
        &self,
        instrument: &MarketInstrument,
        interval: &str,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<AdapterOutcome<GlobalMarketBar>> {
        if !matches!(interval, "1h" | "1d") {
            return Ok(AdapterOutcome::failure(
                ProviderStatus::PermanentError,
                None,
            ));
        }
        let Some(symbol) = instrument
            .provider_aliases
            .get("yahoo")
            .and_then(Value::as_str)
        else {
            return Ok(AdapterOutcome::failure(
                ProviderStatus::PermanentError,
                None,
            ));
        };
        let outcome = self
            .chart(
                symbol,
                interval,
                Some(start.timestamp()),
                Some(end.timestamp()),
                None,
            )
            .await?;
        if !matches!(
            outcome.status,
            ProviderStatus::Success | ProviderStatus::NoData
        ) {
            return Ok(AdapterOutcome::failure(outcome.status, outcome.retry_after));
        }
        let timezone: Tz = instrument
            .timezone
            .parse()
            .context("invalid instrument timezone")?;
        let open_time = session_time(&instrument.session_metadata, "open", NaiveTime::MIN);
        let close_time = session_time(
            &instrument.session_metadata,
            "close",
            NaiveTime::from_hms_opt(23, 59, 0).unwrap(),
        );
        let mut bars = Vec::new();
        for chart in parse_chart(&outcome.items[0])? {
            let local = chart.timestamp.with_timezone(&timezone);
            let session_date = local.date_naive();
            let (starts_at, ends_at) = if interval == "1d" {
                let start_date = if open_time > close_time {
                    session_date - TimeDelta::days(1)
                } else {
                    session_date
                };
                (
                    local_to_utc(timezone, start_date, open_time)?,
                    local_to_utc(timezone, session_date, close_time)?,
                )
            } else {
                (chart.timestamp, chart.timestamp + TimeDelta::hours(1))
            };
            let provider_is_local_per_usd =
                instrument.quote_convention.as_deref() == Some("local_currency_per_usd");
            let (open, high, low, close) = if instrument.asset_class == "fx" {
                normalize_fx_ohlc(
                    chart.open,
                    chart.high,
                    chart.low,
                    chart.close,
                    provider_is_local_per_usd,
                )?
            } else {
                (chart.open, chart.high, chart.low, chart.close)
            };
            bars.push(GlobalMarketBar {
                instrument_key: instrument.instrument_key.clone(),
                interval: interval.into(),
                starts_at,
                ends_at,
                session_date,
                open_price: open,
                high_price: high,
                low_price: low,
                close_price: close,
                volume: chart.volume,
                provider: "yahoo".into(),
            });
        }
        Ok(AdapterOutcome::success(bars))
    }

    pub async fn daily_closes(
        &self,
        symbol: &str,
        range: &str,
    ) -> Result<AdapterOutcome<DailyClose>> {
        let outcome = self.chart(symbol, "1d", None, None, Some(range)).await?;
        if !matches!(
            outcome.status,
            ProviderStatus::Success | ProviderStatus::NoData
        ) {
            return Ok(AdapterOutcome::failure(outcome.status, outcome.retry_after));
        }
        Ok(AdapterOutcome::success(
            parse_chart(&outcome.items[0])?
                .into_iter()
                .map(|bar| DailyClose {
                    date: bar.timestamp.date_naive(),
                    close: bar.close,
                })
                .collect(),
        ))
    }
}

#[async_trait]
impl QuoteProvider for YahooProvider {
    fn provider_name(&self) -> &'static str {
        "yahoo"
    }

    async fn fetch_quote(&self, symbol: &str, session: &str) -> Result<AdapterOutcome<StockQuote>> {
        let outcome = self.chart(symbol, "1m", None, None, Some("1d")).await?;
        if !matches!(
            outcome.status,
            ProviderStatus::Success | ProviderStatus::NoData
        ) {
            return Ok(AdapterOutcome::failure(outcome.status, outcome.retry_after));
        }
        let root = &outcome.items[0];
        let mut bars = parse_chart(root)?;
        let Some(latest) = bars.pop() else {
            return Ok(AdapterOutcome::success(Vec::new()));
        };
        let volume = root
            .pointer("/chart/result/0/meta/regularMarketVolume")
            .and_then(Value::as_i64)
            .or_else(|| latest.volume.map(|volume| volume as i64))
            .unwrap_or(0);
        Ok(AdapterOutcome::success(vec![StockQuote {
            symbol: symbol.into(),
            timestamp: latest.timestamp,
            price: latest.close,
            volume,
            market_session: session.into(),
            provider: "yfinance".into(),
        }]))
    }
}

#[async_trait]
impl FundamentalsProvider for YahooProvider {
    async fn fetch_fundamentals(
        &self,
        symbol: &str,
        sector_baseline: &str,
    ) -> Result<AdapterOutcome<StockMetrics>> {
        let stock = self.daily_history(symbol).await?;
        let baseline = self.daily_history(sector_baseline).await?;
        if stock.status == ProviderStatus::RateLimited
            || baseline.status == ProviderStatus::RateLimited
        {
            return Ok(AdapterOutcome::failure(ProviderStatus::RateLimited, None));
        }
        if stock.items.is_empty() || baseline.items.is_empty() {
            return Ok(AdapterOutcome::success(Vec::new()));
        }
        let pe = self.trailing_pe(symbol).await.unwrap_or(None);
        let baseline_pe = self.trailing_pe(sector_baseline).await.unwrap_or(None);
        Ok(AdapterOutcome::success(
            build_metrics(symbol, pe, &stock.items, baseline_pe, &baseline.items)
                .into_iter()
                .collect(),
        ))
    }
}

pub fn parse_chart(root: &Value) -> Result<Vec<ChartBar>> {
    let result = root
        .pointer("/chart/result/0")
        .context("Yahoo chart result missing")?;
    let timestamps = result
        .get("timestamp")
        .and_then(Value::as_array)
        .context("Yahoo timestamps missing")?;
    let quote = result
        .pointer("/indicators/quote/0")
        .context("Yahoo quote arrays missing")?;
    let opens = array(quote, "open")?;
    let highs = array(quote, "high")?;
    let lows = array(quote, "low")?;
    let closes = array(quote, "close")?;
    let volumes = quote.get("volume").and_then(Value::as_array);
    let length = [
        timestamps.len(),
        opens.len(),
        highs.len(),
        lows.len(),
        closes.len(),
    ]
    .into_iter()
    .min()
    .unwrap_or(0);
    let mut bars = Vec::new();
    for index in 0..length {
        let Some(timestamp) = timestamps[index]
            .as_i64()
            .and_then(|seconds| Utc.timestamp_opt(seconds, 0).single())
        else {
            continue;
        };
        let (Some(open), Some(high), Some(low), Some(close)) = (
            opens[index].as_f64(),
            highs[index].as_f64(),
            lows[index].as_f64(),
            closes[index].as_f64(),
        ) else {
            continue;
        };
        if ![open, high, low, close]
            .iter()
            .all(|value| value.is_finite() && *value > 0.0)
            || low > open.min(close)
            || high < open.max(close)
        {
            continue;
        }
        let volume = volumes
            .and_then(|values| values.get(index))
            .and_then(Value::as_f64)
            .filter(|value| value.is_finite())
            .map(|value| value.max(0.0));
        bars.push(ChartBar {
            timestamp,
            open,
            high,
            low,
            close,
            volume,
        });
    }
    Ok(bars)
}

fn array<'a>(value: &'a Value, key: &str) -> Result<&'a Vec<Value>> {
    value
        .get(key)
        .and_then(Value::as_array)
        .with_context(|| format!("Yahoo {key} array missing"))
}

async fn parse_http_json(response: Response) -> Result<AdapterOutcome<Value>> {
    let status = response.status();
    let retry_after = response
        .headers()
        .get("retry-after")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs);
    if !status.is_success() {
        let provider_status = match status {
            StatusCode::FORBIDDEN => ProviderStatus::Blocked,
            StatusCode::TOO_MANY_REQUESTS => ProviderStatus::RateLimited,
            status if status.is_server_error() => ProviderStatus::TransientError,
            _ => ProviderStatus::PermanentError,
        };
        return Ok(AdapterOutcome::failure(provider_status, retry_after));
    }
    let value: Value = response.json().await?;
    if value
        .pointer("/chart/error")
        .is_some_and(|error| !error.is_null())
    {
        return Ok(AdapterOutcome::failure(
            ProviderStatus::TransientError,
            None,
        ));
    }
    if value.pointer("/chart/result/0").is_none()
        && value.pointer("/quoteSummary/result/0").is_none()
    {
        return Ok(AdapterOutcome::success(Vec::new()));
    }
    Ok(AdapterOutcome::success(vec![value]))
}

fn session_time(
    metadata: &serde_json::Map<String, Value>,
    key: &str,
    fallback: NaiveTime,
) -> NaiveTime {
    metadata
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| NaiveTime::parse_from_str(value, "%H:%M").ok())
        .unwrap_or(fallback)
}

fn local_to_utc(timezone: Tz, date: NaiveDate, time: NaiveTime) -> Result<DateTime<Utc>> {
    timezone
        .from_local_datetime(&date.and_time(time))
        .single()
        .map(|value| value.with_timezone(&Utc))
        .context("ambiguous or nonexistent instrument session time")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chart_parser_skips_non_finite_or_incomplete_rows() {
        let fixture: Value = serde_json::from_str(
            r#"{
          "chart":{"result":[{"timestamp":[1,2,3],"indicators":{"quote":[{
            "open":[10.0,null,4.0],"high":[12.0,4.0,3.0],"low":[9.0,2.0,2.0],
            "close":[11.0,3.0,4.0],"volume":[100,200,-4]
          }]}}],"error":null}}
        "#,
        )
        .unwrap();
        let bars = parse_chart(&fixture).unwrap();
        assert_eq!(bars.len(), 1);
        assert_eq!(bars[0].volume, Some(100.0));
    }
}
