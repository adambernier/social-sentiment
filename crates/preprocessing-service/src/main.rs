mod topic_model;

use anyhow::Result;
use chrono::Utc;
use futures_util::stream::StreamExt;
use lapin::{
    options::{BasicAckOptions, BasicConsumeOptions, BasicPublishOptions, BasicQosOptions, QueueDeclareOptions},
    types::{AMQPValue, FieldTable},
    BasicProperties, Channel, Connection, ConnectionProperties,
};
use social_sentiment_core::{
    cleaner::{clean_text, is_valid},
    config::Config,
    messaging::{dead_letter_queue_name, ERROR_HEADER, ERROR_TYPE_HEADER, ORIGINAL_QUEUE_HEADER, PROCESSING_ATTEMPT_HEADER},
    schemas::{CleanPost, RawPost},
};
use std::sync::Arc;
use topic_model::TopicModel;
use tracing::{error, info, warn};

fn get_attempt_header(delivery: &lapin::message::Delivery) -> i32 {
    delivery
        .properties
        .headers()
        .as_ref()
        .and_then(|h| h.inner().get(PROCESSING_ATTEMPT_HEADER))
        .and_then(|val| match val {
            AMQPValue::ShortShortInt(v) => Some(*v as i32),
            AMQPValue::ShortInt(v) => Some(*v as i32),
            AMQPValue::LongInt(v) => Some(*v),
            AMQPValue::LongLongInt(v) => Some(*v as i32),
            _ => None,
        })
        .unwrap_or(0)
}

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
    headers.insert(ERROR_TYPE_HEADER.into(), AMQPValue::LongString("ProcessingError".into()));
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

async fn retry_or_dead_letter(
    channel: &Channel,
    input_queue: &str,
    msg_bytes: &[u8],
    delivery_tag: u64,
    attempt: i32,
    error_msg: &str,
) -> Result<()> {
    if attempt >= 1 {
        dead_letter_message(channel, input_queue, msg_bytes, delivery_tag, error_msg).await
    } else {
        let mut headers = FieldTable::default();
        headers.insert(PROCESSING_ATTEMPT_HEADER.into(), AMQPValue::LongInt(attempt + 1));

        let props = BasicProperties::default()
            .with_delivery_mode(2)
            .with_headers(headers);

        channel
            .basic_publish("", input_queue, BasicPublishOptions::default(), msg_bytes, props)
            .await?;
        channel.basic_ack(delivery_tag, BasicAckOptions::default()).await?;
        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    info!("Starting Rust preprocessing-service...");

    let config = Config::from_env();
    let model_dir = std::env::var("MODEL_DIR").unwrap_or_else(|_| "preprocessing-service/model_quant".to_string());

    info!("Initializing ONNX Zero-Shot Topic Model from {}...", model_dir);
    let topic_model = Arc::new(TopicModel::new(&model_dir)?);
    info!("Topic Model loaded successfully (hash: {}).", topic_model.model_hash);

    let rabbit_url = config.rabbit_url();
    info!("Connecting to RabbitMQ at {}...", rabbit_url);

    let conn = Connection::connect(&rabbit_url, ConnectionProperties::default()).await?;
    let channel = conn.create_channel().await?;

    channel.basic_qos(10, BasicQosOptions::default()).await?;

    let input_queue = &config.queue_raw_posts;
    let output_queue = &config.queue_clean_posts;
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
            output_queue,
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

    info!("Listening on '{}'. Ready to consume raw posts...", input_queue);

    let mut consumer = channel
        .basic_consume(
            input_queue,
            "preprocessing-consumer",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await?;

    while let Some(delivery_result) = consumer.next().await {
        match delivery_result {
            Ok(delivery) => {
                let body = delivery.data.as_slice();
                let delivery_tag = delivery.delivery_tag;

                let attempt = get_attempt_header(&delivery);

                let raw: RawPost = match serde_json::from_slice(body) {
                    Ok(post) => post,
                    Err(e) => {
                        error!("Malformed raw post message: {}", e);
                        let _ = dead_letter_message(&channel, input_queue, body, delivery_tag, &e.to_string()).await;
                        continue;
                    }
                };

                let cleaned = clean_text(&raw.text);
                if !is_valid(&cleaned) {
                    info!("{}: dropped (too short after cleaning)", raw.id);
                    let _ = channel.basic_ack(delivery_tag, BasicAckOptions::default()).await;
                    continue;
                }

                let topic_model_ref = Arc::clone(&topic_model);
                let cleaned_clone = cleaned.clone();

                let (topic_id, topic_label) = match tokio::task::spawn_blocking(move || {
                    topic_model_ref.predict(&cleaned_clone)
                })
                .await
                {
                    Ok(Ok((id, lbl))) => (id, lbl),
                    Ok(Err(e)) => {
                        error!("Topic model prediction error for {}: {}", raw.id, e);
                        let _ = retry_or_dead_letter(&channel, input_queue, body, delivery_tag, attempt, &e.to_string()).await;
                        continue;
                    }
                    Err(e) => {
                        error!("Topic model thread panic for {}: {}", raw.id, e);
                        let _ = retry_or_dead_letter(&channel, input_queue, body, delivery_tag, attempt, &e.to_string()).await;
                        continue;
                    }
                };

                let now = Utc::now();
                let clean_post = CleanPost {
                    id: raw.id.clone(),
                    symbol: raw.symbol.clone(),
                    platform: raw.platform.clone(),
                    text: cleaned.clone(),
                    timestamp: raw.timestamp,
                    topic_id: Some(topic_id),
                    topic_label: Some(topic_label.clone()),
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

                let clean_bytes = match serde_json::to_vec(&clean_post) {
                    Ok(b) => b,
                    Err(e) => {
                        error!("Failed to serialize clean post {}: {}", raw.id, e);
                        let _ = retry_or_dead_letter(&channel, input_queue, body, delivery_tag, attempt, &e.to_string()).await;
                        continue;
                    }
                };

                let props = BasicProperties::default().with_delivery_mode(2);
                if let Err(e) = channel
                    .basic_publish("", output_queue, BasicPublishOptions::default(), &clean_bytes, props)
                    .await
                {
                    error!("Error publishing clean post {}: {}", raw.id, e);
                    let _ = retry_or_dead_letter(&channel, input_queue, body, delivery_tag, attempt, &e.to_string()).await;
                    continue;
                }

                let _ = channel.basic_ack(delivery_tag, BasicAckOptions::default()).await;
                info!(
                    "{} ({}): topic='{}', text='{}'",
                    raw.id,
                    raw.symbol,
                    topic_label,
                    cleaned.chars().take(70).collect::<String>()
                );
            }
            Err(e) => {
                warn!("Error receiving delivery from RabbitMQ: {}", e);
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            }
        }
    }

    Ok(())
}
