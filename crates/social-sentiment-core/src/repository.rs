//! SQLx repositories shared by Rust API, workers, and producers.

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use sqlx::{types::Json, PgPool, Row};

use crate::schemas::{ScoredPost, StockMetrics, StockQuote};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TrackedSymbol {
    pub symbol: String,
    pub keywords: Vec<String>,
    pub future: Option<String>,
    pub sector: Option<String>,
    pub require_uppercase: bool,
    pub require_cashtag: bool,
    pub block_phrases: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct GlobalMarketBar {
    pub instrument_key: String,
    pub interval: String,
    pub starts_at: DateTime<Utc>,
    pub ends_at: DateTime<Utc>,
    pub session_date: NaiveDate,
    pub open_price: f64,
    pub high_price: f64,
    pub low_price: f64,
    pub close_price: f64,
    pub volume: Option<f64>,
    pub provider: String,
}

#[derive(Clone)]
pub struct Repository {
    pool: PgPool,
}

impl Repository {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    pub async fn active_symbols(&self) -> Result<Vec<TrackedSymbol>, sqlx::Error> {
        let rows = sqlx::query(
            r#"
            SELECT symbol, keywords, future, sector, require_uppercase,
                   require_cashtag, block_phrases
            FROM tracked_symbols
            WHERE is_active
            ORDER BY symbol
            "#,
        )
        .fetch_all(&self.pool)
        .await?;
        Ok(rows
            .into_iter()
            .map(|row| TrackedSymbol {
                symbol: row.get("symbol"),
                keywords: row.get::<Json<Vec<String>>, _>("keywords").0,
                future: row.get("future"),
                sector: row.get("sector"),
                require_uppercase: row.get("require_uppercase"),
                require_cashtag: row.get("require_cashtag"),
                block_phrases: row.get::<Json<Vec<String>>, _>("block_phrases").0,
            })
            .collect())
    }

    pub async fn insert_scored_post(&self, post: &ScoredPost) -> Result<bool, sqlx::Error> {
        post.validate().map_err(sqlx::Error::Protocol)?;
        let result = sqlx::query(
            r#"
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores,
                topic_id, topic_label, scored_at, engagement
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (platform, id, symbol) DO NOTHING
            "#,
        )
        .bind(&post.clean.id)
        .bind(&post.clean.symbol)
        .bind(&post.clean.platform)
        .bind(&post.clean.text)
        .bind(post.clean.timestamp)
        .bind(&post.sentiment)
        .bind(Json(&post.scores))
        .bind(post.clean.topic_id)
        .bind(&post.clean.topic_label)
        .bind(post.sentiment_scored_at)
        .bind(post.clean.engagement)
        .execute(&self.pool)
        .await?;
        Ok(result.rows_affected() == 1)
    }

    pub async fn upsert_quote(&self, quote: &StockQuote) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO stock_quotes (symbol, timestamp, price, volume, market_session)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (symbol, timestamp) DO UPDATE SET
                price = EXCLUDED.price,
                volume = EXCLUDED.volume,
                market_session = EXCLUDED.market_session
            "#,
        )
        .bind(&quote.symbol)
        .bind(quote.timestamp)
        .bind(quote.price)
        .bind(quote.volume)
        .bind(&quote.market_session)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn upsert_metrics(&self, metrics: &StockMetrics) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO stock_metrics (
                symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y,
                pe_relative_sector, beta_relative_sector, return_relative_sector
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol) DO UPDATE SET
                pe_ratio = EXCLUDED.pe_ratio,
                beta = EXCLUDED.beta,
                avg_return_1y = EXCLUDED.avg_return_1y,
                inflation_adj_return_1y = EXCLUDED.inflation_adj_return_1y,
                pe_relative_sector = EXCLUDED.pe_relative_sector,
                beta_relative_sector = EXCLUDED.beta_relative_sector,
                return_relative_sector = EXCLUDED.return_relative_sector,
                updated_at = NOW()
            "#,
        )
        .bind(&metrics.symbol)
        .bind(metrics.pe_ratio)
        .bind(metrics.beta)
        .bind(metrics.avg_return_1y)
        .bind(metrics.inflation_adj_return_1y)
        .bind(metrics.pe_relative_sector)
        .bind(metrics.beta_relative_sector)
        .bind(metrics.return_relative_sector)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn upsert_global_bar(&self, bar: &GlobalMarketBar) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO global_market_bars (
                instrument_key, interval, starts_at, ends_at, session_date,
                open_price, high_price, low_price, close_price, volume, provider
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (instrument_key, interval, starts_at) DO UPDATE SET
                ends_at = EXCLUDED.ends_at,
                session_date = EXCLUDED.session_date,
                open_price = EXCLUDED.open_price,
                high_price = EXCLUDED.high_price,
                low_price = EXCLUDED.low_price,
                close_price = EXCLUDED.close_price,
                volume = EXCLUDED.volume,
                provider = EXCLUDED.provider,
                fetched_at = NOW()
            "#,
        )
        .bind(&bar.instrument_key)
        .bind(&bar.interval)
        .bind(bar.starts_at)
        .bind(bar.ends_at)
        .bind(bar.session_date)
        .bind(bar.open_price)
        .bind(bar.high_price)
        .bind(bar.low_price)
        .bind(bar.close_price)
        .bind(bar.volume)
        .bind(&bar.provider)
        .execute(&self.pool)
        .await?;
        Ok(())
    }
}
