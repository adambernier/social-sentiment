use anyhow::{anyhow, Result};
use chrono::Utc;
use futures_util::stream::StreamExt;
use lapin::{
    options::{
        BasicAckOptions, BasicConsumeOptions, BasicQosOptions, ConfirmSelectOptions,
        QueueDeclareOptions,
    },
    types::{AMQPValue, FieldTable},
    BasicProperties, Channel, Connection, ConnectionProperties,
};
use service_preprocessing::topic_model::TopicModel;
use social_sentiment_core::{
    cleaner::{clean_text, is_valid},
    config::Config,
    messaging::{
        dead_letter_queue_name, publish_confirmed, truncate_error, ERROR_HEADER, ERROR_TYPE_HEADER,
        ORIGINAL_QUEUE_HEADER, PROCESSING_ATTEMPT_HEADER,
    },
    observability::{increment_errors, increment_processed, start_metrics_server},
    runtime::shutdown_signal,
    schemas::{CleanPost, RawPost},
};
use std::sync::Arc;
use std::time::Duration;
use tracing::{error, info, warn};

fn get_attempt_header(delivery: &lapin::message::Delivery) -> i32 {
    delivery
        .properties
        .headers()
        .as_ref()
        .and_then(|headers| headers.inner().get(PROCESSING_ATTEMPT_HEADER))
        .and_then(|value| match value {
            AMQPValue::ShortShortInt(value) => Some(i32::from(*value)),
            AMQPValue::ShortInt(value) => Some(i32::from(*value)),
            AMQPValue::LongInt(value) => Some(*value),
            AMQPValue::LongLongInt(value) => i32::try_from(*value).ok(),
            _ => None,
        })
        .unwrap_or(0)
}

async fn dead_letter_message(
    channel: &Channel,
    input_queue: &str,
    payload: &[u8],
    delivery_tag: u64,
    error_message: &str,
) -> Result<()> {
    let mut headers = FieldTable::default();
    headers.insert(
        ORIGINAL_QUEUE_HEADER.into(),
        AMQPValue::LongString(input_queue.into()),
    );
    headers.insert(
        ERROR_TYPE_HEADER.into(),
        AMQPValue::LongString("ProcessingError".into()),
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
        .basic_ack(delivery_tag, BasicAckOptions::default())
        .await?;
    increment_errors(1);
    Ok(())
}

async fn retry_or_dead_letter(
    channel: &Channel,
    input_queue: &str,
    payload: &[u8],
    delivery_tag: u64,
    attempt: i32,
    error_message: &str,
) -> Result<()> {
    if attempt >= 1 {
        return dead_letter_message(channel, input_queue, payload, delivery_tag, error_message)
            .await;
    }

    let mut headers = FieldTable::default();
    headers.insert(
        PROCESSING_ATTEMPT_HEADER.into(),
        AMQPValue::LongInt(attempt + 1),
    );
    let properties = BasicProperties::default()
        .with_delivery_mode(2)
        .with_content_type("application/json".into())
        .with_headers(headers);
    publish_confirmed(channel, input_queue, payload, properties).await?;
    channel
        .basic_ack(delivery_tag, BasicAckOptions::default())
        .await?;
    increment_errors(1);
    Ok(())
}

async fn run_consumer_session(config: &Config, topic_model: Arc<TopicModel>) -> Result<()> {
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
    channel.basic_qos(10, BasicQosOptions::default()).await?;

    let input_queue = &config.queue_raw_posts;
    let output_queue = &config.queue_clean_posts;
    let dead_letter_queue = dead_letter_queue_name(input_queue);
    for queue_name in [
        input_queue.as_str(),
        output_queue.as_str(),
        dead_letter_queue.as_str(),
    ] {
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
            "preprocessing-consumer",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await?;
    info!(queue = %input_queue, "ready to consume raw posts");

    while let Some(delivery_result) = consumer.next().await {
        let delivery = delivery_result?;
        let payload = delivery.data.as_slice();
        let delivery_tag = delivery.delivery_tag;
        let attempt = get_attempt_header(&delivery);

        let raw: RawPost = match serde_json::from_slice(payload) {
            Ok(post) => post,
            Err(err) => {
                error!(%err, "malformed raw post message");
                dead_letter_message(
                    &channel,
                    input_queue,
                    payload,
                    delivery_tag,
                    &err.to_string(),
                )
                .await?;
                continue;
            }
        };

        let cleaned = clean_text(&raw.text);
        if !is_valid(&cleaned) {
            channel
                .basic_ack(delivery_tag, BasicAckOptions::default())
                .await?;
            increment_processed(1);
            continue;
        }

        let model = Arc::clone(&topic_model);
        let model_input = cleaned.clone();
        let prediction = tokio::task::spawn_blocking(move || model.predict(&model_input)).await;
        let (topic_id, topic_label) = match prediction {
            Ok(Ok(result)) => result,
            Ok(Err(err)) => {
                retry_or_dead_letter(
                    &channel,
                    input_queue,
                    payload,
                    delivery_tag,
                    attempt,
                    &err.to_string(),
                )
                .await?;
                continue;
            }
            Err(err) => {
                retry_or_dead_letter(
                    &channel,
                    input_queue,
                    payload,
                    delivery_tag,
                    attempt,
                    &err.to_string(),
                )
                .await?;
                continue;
            }
        };

        let now = Utc::now();
        let clean_post = CleanPost {
            id: raw.id.clone(),
            symbol: raw.symbol.clone(),
            platform: raw.platform,
            text: cleaned,
            timestamp: raw.timestamp,
            topic_id: Some(topic_id),
            topic_label: Some(topic_label),
            engagement: raw.engagement,
            ingested_at: raw.ingested_at,
            engagement_observed_at: raw.engagement_observed_at,
            source_schema_version: raw.source_schema_version,
            pipeline_git_commit: raw.pipeline_git_commit,
            cleaned_at: now,
            topic_scored_at: now,
            topic_model_version: topic_model.version.clone(),
            topic_model_hash: topic_model.model_hash.clone(),
        };
        let clean_payload = serde_json::to_vec(&clean_post)?;
        let properties = BasicProperties::default()
            .with_delivery_mode(2)
            .with_content_type("application/json".into());

        if let Err(err) =
            publish_confirmed(&channel, output_queue, &clean_payload, properties).await
        {
            retry_or_dead_letter(
                &channel,
                input_queue,
                payload,
                delivery_tag,
                attempt,
                &err.to_string(),
            )
            .await?;
            continue;
        }
        channel
            .basic_ack(delivery_tag, BasicAckOptions::default())
            .await?;
        increment_processed(1);
        info!(post_id = %raw.id, symbol = %raw.symbol, "preprocessed post");
    }

    Err(anyhow!("RabbitMQ consumer stream ended"))
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    start_metrics_server(8007, "preprocessing");

    let config = Config::from_env();
    let model_dir = std::env::var("MODEL_DIR")
        .unwrap_or_else(|_| "preprocessing-service/model_quant".to_string());
    let topic_model = Arc::new(TopicModel::new(&model_dir)?);
    info!(model_hash = %topic_model.model_hash, "topic model loaded");

    loop {
        tokio::select! {
            result = run_consumer_session(&config, Arc::clone(&topic_model)) => {
                warn!(error = ?result.err(), "consumer session ended; reconnecting");
            }
            () = shutdown_signal() => {
                info!("shutdown requested; unacknowledged messages will be requeued");
                return Ok(());
            }
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}
