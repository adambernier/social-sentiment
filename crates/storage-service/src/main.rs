mod db;

use anyhow::{anyhow, Result};
use chrono::{DateTime, Duration, Timelike, Utc};
use db::DatabaseService;
use futures_util::stream::StreamExt;
use lapin::{
    options::{
        BasicAckOptions, BasicConsumeOptions, BasicQosOptions, ConfirmSelectOptions,
        QueueDeclareOptions,
    },
    types::{AMQPValue, FieldTable},
    BasicProperties, Channel, Connection, ConnectionProperties,
};
use social_sentiment_core::{
    config::Config,
    messaging::{
        dead_letter_queue_name, publish_confirmed, truncate_error, ERROR_HEADER, ERROR_TYPE_HEADER,
        ORIGINAL_QUEUE_HEADER,
    },
    observability::{increment_errors, increment_processed, start_metrics_server},
    runtime::shutdown_signal,
    schemas::ScoredPost,
};
use std::sync::Arc;
use std::time::Duration as StdDuration;
use tracing::{error, info, warn};

const BATCH_SIZE: usize = 100;
const BATCH_TIMEOUT: StdDuration = StdDuration::from_secs(1);

fn is_permanent_sqlstate(code: &str) -> bool {
    code.starts_with("22") || code.starts_with("23")
}

fn is_permanent_post_error(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<sqlx::Error>()
        .and_then(sqlx::Error::as_database_error)
        .and_then(|database_error| database_error.code())
        .is_some_and(|code| is_permanent_sqlstate(&code))
}

async fn dead_letter_message(
    channel: &Channel,
    input_queue: &str,
    payload: &[u8],
    delivery: &lapin::message::Delivery,
    error_message: &str,
) -> Result<()> {
    let mut headers = delivery
        .properties
        .headers()
        .as_ref()
        .cloned()
        .unwrap_or_default();
    headers.insert(
        ORIGINAL_QUEUE_HEADER.into(),
        AMQPValue::LongString(input_queue.into()),
    );
    headers.insert(
        ERROR_TYPE_HEADER.into(),
        AMQPValue::LongString("PermanentDatabaseError".into()),
    );
    headers.insert(
        ERROR_HEADER.into(),
        AMQPValue::LongString(truncate_error(error_message, 500).into()),
    );
    let properties = BasicProperties::default()
        .with_delivery_mode(2)
        .with_content_type("application/json".into())
        .with_headers(headers);
    publish_confirmed(
        channel,
        &dead_letter_queue_name(input_queue),
        payload,
        properties,
    )
    .await?;
    channel
        .basic_ack(delivery.delivery_tag, BasicAckOptions::default())
        .await?;
    increment_errors(1);
    Ok(())
}

async fn persist_scored_individually(
    posts: &[ScoredPost],
    deliveries: &[(lapin::message::Delivery, Vec<u8>)],
    database: &DatabaseService,
    channel: &Channel,
    input_queue: &str,
) -> Result<(u64, u64)> {
    let mut stored = 0_u64;
    let mut dead_lettered = 0_u64;

    for (post, (delivery, payload)) in posts.iter().zip(deliveries) {
        match database
            .insert_scored_batch(std::slice::from_ref(post))
            .await
        {
            Ok(count) => {
                channel
                    .basic_ack(delivery.delivery_tag, BasicAckOptions::default())
                    .await?;
                stored += count;
                increment_processed(1);
            }
            Err(error) if is_permanent_post_error(&error) => {
                dead_letter_message(channel, input_queue, payload, delivery, &error.to_string())
                    .await?;
                dead_lettered += 1;
            }
            Err(error) => return Err(error),
        }
    }
    Ok((stored, dead_lettered))
}

async fn persist_scored_batch(
    posts: &[ScoredPost],
    deliveries: &[(lapin::message::Delivery, Vec<u8>)],
    database: &DatabaseService,
    channel: &Channel,
    input_queue: &str,
) -> Result<(u64, u64)> {
    match database.insert_scored_batch(posts).await {
        Ok(count) => {
            for (delivery, _) in deliveries {
                channel
                    .basic_ack(delivery.delivery_tag, BasicAckOptions::default())
                    .await?;
            }
            increment_processed(deliveries.len() as u64);
            Ok((count, 0))
        }
        Err(error) if is_permanent_post_error(&error) => {
            warn!(posts = posts.len(), %error, "isolating permanently invalid database input");
            persist_scored_individually(posts, deliveries, database, channel, input_queue).await
        }
        Err(error) => Err(error),
    }
}

async fn run_rollup_scheduler(database: Arc<DatabaseService>, config: Config) {
    tokio::time::sleep(StdDuration::from_secs(60)).await;

    loop {
        let now = Utc::now();
        let post_cutoff = (now - Duration::days(i64::from(config.post_retention_days)))
            .date_naive()
            .and_hms_opt(now.time().hour(), 0, 0)
            .map(|value| DateTime::<Utc>::from_naive_utc_and_offset(value, Utc))
            .unwrap_or(now - Duration::days(i64::from(config.post_retention_days)));
        let quote_cutoff = now - Duration::days(i64::from(config.quote_retention_days));

        if let Err(error) = database
            .rollup_and_prune_posts(
                post_cutoff,
                &config.raw_archive_platforms,
                config.raw_archive_sample_rate,
                config.raw_archive_challenge_engagement,
                config.raw_archive_challenge_abs_signal,
            )
            .await
        {
            error!(%error, "post retention rollup failed");
        }
        if let Err(error) = database.rollup_and_prune_quotes(quote_cutoff).await {
            error!(%error, "quote retention rollup failed");
        }
        if let Err(error) = database
            .prune_global_context(
                now - Duration::days(180),
                now - Duration::days(365 * 5),
                now - Duration::days(365),
            )
            .await
        {
            error!(%error, "global-context retention failed");
        }

        tokio::time::sleep(StdDuration::from_secs(24 * 60 * 60)).await;
    }
}

async fn run_consumer_session(config: &Config, database: &DatabaseService) -> Result<()> {
    info!(
        host = %config.rabbit_host,
        port = config.rabbit_port,
        "connecting to RabbitMQ"
    );
    let connection =
        Connection::connect(&config.rabbit_url(), ConnectionProperties::default()).await?;
    let channel = connection.create_channel().await?;
    channel
        .confirm_select(ConfirmSelectOptions::default())
        .await?;
    channel
        .basic_qos((BATCH_SIZE * 2) as u16, BasicQosOptions::default())
        .await?;

    let input_queue = &config.queue_scored_posts;
    let dead_letter_queue = dead_letter_queue_name(input_queue);
    for queue_name in [input_queue.as_str(), dead_letter_queue.as_str()] {
        channel
            .queue_declare(
                queue_name,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
    }

    let mut consumer = channel
        .basic_consume(
            input_queue,
            "storage-consumer",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await?;
    let mut deliveries = Vec::with_capacity(BATCH_SIZE);
    let mut posts = Vec::with_capacity(BATCH_SIZE);

    loop {
        let timeout = tokio::time::sleep(BATCH_TIMEOUT);
        tokio::pin!(timeout);
        tokio::select! {
            delivery_result = consumer.next() => {
                let delivery = delivery_result.ok_or_else(|| anyhow!("RabbitMQ consumer stream ended"))??;
                let payload = delivery.data.clone();
                match serde_json::from_slice::<ScoredPost>(&payload) {
                    Ok(post) => {
                        if let Err(validation_error) = post.validate() {
                            dead_letter_message(
                                &channel,
                                input_queue,
                                &payload,
                                &delivery,
                                &validation_error,
                            ).await?;
                            continue;
                        }
                        deliveries.push((delivery, payload));
                        posts.push(post);
                        if posts.len() < BATCH_SIZE {
                            continue;
                        }
                    }
                    Err(error) => {
                        dead_letter_message(
                            &channel,
                            input_queue,
                            &payload,
                            &delivery,
                            &error.to_string(),
                        ).await?;
                        continue;
                    }
                }
            }
            () = &mut timeout => {}
        }

        if !posts.is_empty() {
            persist_scored_batch(
                &std::mem::take(&mut posts),
                &std::mem::take(&mut deliveries),
                database,
                &channel,
                input_queue,
            )
            .await?;
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    start_metrics_server(8008, "storage");

    let config = Config::from_env();
    let database = Arc::new(DatabaseService::connect(&config.database_dsn).await?);
    let scheduler = tokio::spawn(run_rollup_scheduler(Arc::clone(&database), config.clone()));

    loop {
        tokio::select! {
            result = run_consumer_session(&config, &database) => {
                warn!(error = ?result.err(), "consumer session ended; reconnecting");
            }
            () = shutdown_signal() => {
                info!("shutdown requested; unacknowledged messages will be requeued");
                scheduler.abort();
                return Ok(());
            }
        }
        tokio::time::sleep(StdDuration::from_secs(5)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::is_permanent_sqlstate;

    #[test]
    fn sqlstate_data_and_integrity_errors_are_permanent() {
        assert!(is_permanent_sqlstate("22001"));
        assert!(is_permanent_sqlstate("23505"));
        assert!(!is_permanent_sqlstate("08006"));
        assert!(!is_permanent_sqlstate("40001"));
    }
}
