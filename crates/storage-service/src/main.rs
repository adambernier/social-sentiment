mod db;

use anyhow::Result;
use chrono::{DateTime, Duration, Timelike, Utc};
use db::DatabaseService;
use futures_util::stream::StreamExt;
use lapin::{
    options::{BasicAckOptions, BasicConsumeOptions, BasicPublishOptions, BasicQosOptions, QueueDeclareOptions},
    types::{AMQPValue, FieldTable},
    BasicProperties, Channel, Connection, ConnectionProperties,
};
use social_sentiment_core::{
    config::Config,
    messaging::{dead_letter_queue_name, ERROR_HEADER, ERROR_TYPE_HEADER, ORIGINAL_QUEUE_HEADER},
    schemas::ScoredPost,
};
use std::sync::Arc;
use tokio::time::sleep;
use tracing::{error, info, warn};

const BATCH_SIZE: usize = 100;
const BATCH_TIMEOUT_MS: u64 = 1000;

async fn dead_letter_message(
    channel: &Channel,
    input_queue: &str,
    msg_bytes: &[u8],
    delivery_tag: u64,
    error_msg: &str,
) -> Result<()> {
    let dlq_name = dead_letter_queue_name(input_queue);
    let mut headers = FieldTable::default();
    headers.insert(ORIGINAL_QUEUE_HEADER.into(), AMQPValue::LongString(input_queue.into()));
    headers.insert(ERROR_TYPE_HEADER.into(), AMQPValue::LongString("PermanentDatabaseError".into()));
    headers.insert(ERROR_HEADER.into(), AMQPValue::LongString(error_msg.into()));

    let props = BasicProperties::default()
        .with_delivery_mode(2) // persistent
        .with_headers(headers);

    channel
        .basic_publish("", &dlq_name, BasicPublishOptions::default(), msg_bytes, props)
        .await?;
    channel.basic_ack(delivery_tag, BasicAckOptions::default()).await?;
    Ok(())
}

async fn persist_scored_individually(
    posts: &[ScoredPost],
    deliveries: &[(lapin::message::Delivery, Vec<u8>)],
    db: &DatabaseService,
    channel: &Channel,
    input_queue: &str,
) -> (u64, u64) {
    let mut stored = 0u64;
    let mut dead_lettered = 0u64;

    for (post, (delivery, body)) in posts.iter().zip(deliveries) {
        match db.insert_scored_batch(&[post.clone()]).await {
            Ok(count) => {
                let _ = channel.basic_ack(delivery.delivery_tag, BasicAckOptions::default()).await;
                stored += count;
            }
            Err(e) => {
                error!(
                    "Post {} caused a permanent database error; moving to DLQ: {}",
                    post.clean.id, e
                );
                let _ = dead_letter_message(channel, input_queue, body, delivery.delivery_tag, &e.to_string()).await;
                dead_lettered += 1;
            }
        }
    }
    (stored, dead_lettered)
}

async fn persist_scored_batch(
    posts: &[ScoredPost],
    deliveries: &[(lapin::message::Delivery, Vec<u8>)],
    db: &DatabaseService,
    channel: &Channel,
    input_queue: &str,
) -> Result<(u64, u64)> {
    match db.insert_scored_batch(posts).await {
        Ok(count) => {
            for (delivery, _) in deliveries {
                let _ = channel.basic_ack(delivery.delivery_tag, BasicAckOptions::default()).await;
            }
            Ok((count, 0))
        }
        Err(e) => {
            warn!(
                "Batch insert hit database error; isolating {} posts individually: {}",
                posts.len(),
                e
            );
            let (stored, dead_lettered) = persist_scored_individually(posts, deliveries, db, channel, input_queue).await;
            Ok((stored, dead_lettered))
        }
    }
}

async fn run_rollup_scheduler(db: Arc<DatabaseService>, post_retention_days: i64, quote_retention_days: i64) {
    // Wait 60 seconds after startup before running retention
    sleep(std::time::Duration::from_secs(60)).await;

    let archive_platforms = vec!["bluesky".to_string(), "reddit".to_string(), "stocktwits".to_string()];

    loop {
        info!("Starting scheduled database rollup and prune...");
        let now = Utc::now();

        let post_cutoff = (now - Duration::days(post_retention_days))
            .date_naive()
            .and_hms_opt(now.time().hour(), 0, 0)
            .map(|dt| DateTime::<Utc>::from_naive_utc_and_offset(dt, Utc))
            .unwrap_or(now - Duration::days(post_retention_days));

        let quote_cutoff = now - Duration::days(quote_retention_days);

        match db
            .rollup_and_prune_posts(post_cutoff, &archive_platforms, 0.01, 100, 0.8)
            .await
        {
            Ok((rolled_up, pruned_posts)) => {
                info!(
                    "Post retention completed: rolled up {} aggregation rows, pruned {} posts.",
                    rolled_up, pruned_posts
                );
            }
            Err(e) => {
                error!("Error in post retention rollup: {}", e);
            }
        }

        match db.rollup_and_prune_quotes(quote_cutoff).await {
            Ok((hourly_facts, daily_facts, pruned_quotes)) => {
                info!(
                    "Quote retention completed: preserved {} hourly & {} daily facts, pruned {} quotes.",
                    hourly_facts, daily_facts, pruned_quotes
                );
            }
            Err(e) => {
                error!("Error in quote retention rollup: {}", e);
            }
        }

        let hourly_cutoff = now - Duration::days(180);
        let daily_cutoff = now - Duration::days(365 * 5);
        let event_cutoff = now - Duration::days(365);

        match db.prune_global_context(hourly_cutoff, daily_cutoff, event_cutoff).await {
            Ok((h, d, e)) => {
                info!(
                    "Global context prune completed: {} hourly bars, {} daily bars, {} events pruned.",
                    h, d, e
                );
            }
            Err(err) => {
                error!("Error in global context prune: {}", err);
            }
        }

        sleep(std::time::Duration::from_secs(24 * 3600)).await;
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    info!("Starting Rust storage-service...");

    let config = Config::from_env();

    info!("Connecting to PostgreSQL database...");
    let db = Arc::new(DatabaseService::connect(&config.database_dsn).await?);
    info!("Database connection established.");

    let post_retention_days: i64 = std::env::var("POST_RETENTION_DAYS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(14);
    let quote_retention_days: i64 = std::env::var("QUOTE_RETENTION_DAYS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(90);

    // Spawn 24h retention rollup scheduler
    let db_rollup_ref = Arc::clone(&db);
    tokio::spawn(async move {
        run_rollup_scheduler(db_rollup_ref, post_retention_days, quote_retention_days).await;
    });

    let rabbit_url = config.rabbit_url();
    info!("Connecting to RabbitMQ at {}...", rabbit_url);

    let conn = Connection::connect(&rabbit_url, ConnectionProperties::default()).await?;
    let channel = conn.create_channel().await?;

    channel.basic_qos((BATCH_SIZE * 2) as u16, BasicQosOptions::default()).await?;

    let input_queue = &config.queue_scored_posts;
    let dlq = dead_letter_queue_name(input_queue);

    channel
        .queue_declare(
            input_queue,
            QueueDeclareOptions { durable: true, ..Default::default() },
            FieldTable::default(),
        )
        .await?;

    channel
        .queue_declare(
            &dlq,
            QueueDeclareOptions { durable: true, ..Default::default() },
            FieldTable::default(),
        )
        .await?;

    info!("Listening on '{}'. Ready to store scored posts...", input_queue);

    let mut consumer = channel
        .basic_consume(
            input_queue,
            "storage-consumer",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await?;

    let mut pending_deliveries = Vec::with_capacity(BATCH_SIZE);
    let mut pending_posts = Vec::with_capacity(BATCH_SIZE);

    loop {
        let timeout = tokio::time::sleep(std::time::Duration::from_millis(BATCH_TIMEOUT_MS));
        tokio::pin!(timeout);

        tokio::select! {
            delivery_opt = consumer.next() => {
                match delivery_opt {
                    Some(Ok(delivery)) => {
                        let body = delivery.data.clone();
                        let scored_post: ScoredPost = match serde_json::from_slice(&body) {
                            Ok(post) => post,
                            Err(e) => {
                                error!("Malformed scored post message: {}", e);
                                let _ = dead_letter_message(&channel, input_queue, &body, delivery.delivery_tag, &e.to_string()).await;
                                continue;
                            }
                        };

                        pending_deliveries.push((delivery, body));
                        pending_posts.push(scored_post);

                        if pending_posts.len() < BATCH_SIZE {
                            continue;
                        }
                    }
                    Some(Err(e)) => {
                        warn!("Error receiving delivery from RabbitMQ: {}", e);
                        sleep(std::time::Duration::from_secs(1)).await;
                        continue;
                    }
                    None => {
                        info!("Consumer stream ended.");
                        break;
                    }
                }
            }
            _ = &mut timeout => {
                // Timeout fired, process current batch
            }
        }

        if pending_posts.is_empty() {
            continue;
        }

        let batch_posts = std::mem::take(&mut pending_posts);
        let batch_deliveries = std::mem::take(&mut pending_deliveries);

        info!("Inserting batch of {} posts...", batch_posts.len());
        match persist_scored_batch(&batch_posts, &batch_deliveries, &db, &channel, input_queue).await {
            Ok((stored, dead_lettered)) => {
                info!("Batch handled: {} posts stored, {} dead-lettered.", stored, dead_lettered);
            }
            Err(e) => {
                error!("Storage batch fatal error: {}", e);
            }
        }
    }

    Ok(())
}
