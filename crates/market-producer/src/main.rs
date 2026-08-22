use anyhow::{Context, Result};
use chrono::{DateTime, Utc};

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let args = std::env::args().collect::<Vec<_>>();
    match args.get(1).map(String::as_str) {
        Some("backfill") => {
            let start = parse_datetime(args.get(2), "backfill start")?;
            let end = parse_datetime(args.get(3), "backfill end")?;
            let interval = args.get(4).map(String::as_str).unwrap_or("1d");
            market_producer::runtime::backfill_global(start, end, interval).await
        }
        Some("sync-catalog") => market_producer::runtime::sync_catalog().await,
        Some(command) => anyhow::bail!("unknown market producer command: {command}"),
        None => market_producer::runtime::run().await,
    }
}

fn parse_datetime(value: Option<&String>, label: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value.context(format!("missing {label}"))?)
        .map(|value| value.with_timezone(&Utc))
        .with_context(|| format!("invalid {label}; expected RFC3339"))
}
