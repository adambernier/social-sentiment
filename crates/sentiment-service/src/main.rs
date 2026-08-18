mod sentiment_model;

use anyhow::Result;
use chrono::Utc;
use futures_util::stream::StreamExt;
use lapin::{
    options::{BasicAckOptions, BasicConsumeOptions, BasicPublishOptions, BasicQosOptions, QueueDeclareOptions},
    types::{AMQPValue, FieldTable},
    BasicProperties, Channel, Connection, ConnectionProperties,
};
use social_sentiment_core::{
    config::Config,
    messaging::{dead_letter_queue_name, ERROR_HEADER, ERROR_TYPE_HEADER, ORIGINAL_QUEUE_HEADER, PROCESSING_ATTEMPT_HEADER},
    schemas::{CleanPost, ScoredPost},
};
use std::sync::Arc;
use std::time::Duration;
use sentiment_model::SentimentModel;
use tracing::{error, info, warn};

const BATCH_SIZE: usize = 32;
const BATCH_TIMEOUT_MS: u64 = 1000;

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
    info!("Starting Rust sentiment-service...");

    let config = Config::from_env();
    let model_dir = std::env::var("MODEL_DIR").unwrap_or_else(|_| "sentiment-service/model_quant".to_string());

    info!("Initializing ONNX FinTwitBERT Sentiment Model from {}...", model_dir);
    let sentiment_model = Arc::new(SentimentModel::new(&model_dir)?);
    info!("Sentiment Model loaded successfully (hash: {}).", sentiment_model.model_hash);

    let rabbit_url = config.rabbit_url();
    info!("Connecting to RabbitMQ at {}...", rabbit_url);

    let conn = Connection::connect(&rabbit_url, ConnectionProperties::default()).await?;
    let channel = conn.create_channel().await?;

    // Set prefetch to double batch size for buffering
    channel.basic_qos((BATCH_SIZE * 2) as u16, BasicQosOptions::default()).await?;

    let input_queue = &config.queue_clean_posts;
    let output_queue = &config.queue_scored_posts;
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

    info!("Listening on '{}'. Ready to consume clean posts...", input_queue);

    let mut consumer = channel
        .basic_consume(
            input_queue,
            "sentiment-consumer",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await?;

    let mut pending_deliveries = Vec::with_capacity(BATCH_SIZE);
    let mut pending_posts = Vec::with_capacity(BATCH_SIZE);

    loop {
        let timeout = tokio::time::sleep(Duration::from_millis(BATCH_TIMEOUT_MS));
        tokio::pin!(timeout);

        tokio::select! {
            delivery_opt = consumer.next() => {
                match delivery_opt {
                    Some(Ok(delivery)) => {
                        let body = delivery.data.clone();
                        let clean_post: CleanPost = match serde_json::from_slice(&body) {
                            Ok(post) => post,
                            Err(e) => {
                                error!("Malformed clean post message: {}", e);
                                let _ = dead_letter_message(&channel, input_queue, &body, delivery.delivery_tag, &e.to_string()).await;
                                continue;
                            }
                        };

                        pending_deliveries.push((delivery, body));
                        pending_posts.push(clean_post);

                        if pending_posts.len() < BATCH_SIZE {
                            continue;
                        }
                    }
                    Some(Err(e)) => {
                        warn!("Error receiving delivery from RabbitMQ: {}", e);
                        tokio::time::sleep(Duration::from_secs(1)).await;
                        continue;
                    }
                    None => {
                        info!("Consumer stream ended.");
                        break;
                    }
                }
            }
            _ = &mut timeout => {
                // Timeout fired, process whatever batch we have accumulated
            }
        }

        if pending_posts.is_empty() {
            continue;
        }

        let batch_posts = std::mem::take(&mut pending_posts);
        let batch_deliveries = std::mem::take(&mut pending_deliveries);

        let texts: Vec<String> = batch_posts.iter().map(|p| p.text.clone()).collect();
        let model_ref = Arc::clone(&sentiment_model);

        let results = match tokio::task::spawn_blocking(move || model_ref.predict_batch(&texts)).await {
            Ok(Ok(res)) => res,
            Ok(Err(e)) => {
                error!("Sentiment prediction batch error: {}", e);
                for (del, body) in batch_deliveries {
                    let attempt = get_attempt_header(&del);
                    let _ = retry_or_dead_letter(&channel, input_queue, &body, del.delivery_tag, attempt, &e.to_string()).await;
                }
                continue;
            }
            Err(e) => {
                error!("Sentiment model thread panic: {}", e);
                for (del, body) in batch_deliveries {
                    let attempt = get_attempt_header(&del);
                    let _ = retry_or_dead_letter(&channel, input_queue, &body, del.delivery_tag, attempt, &e.to_string()).await;
                }
                continue;
            }
        };

        if results.len() != batch_posts.len() {
            let err_msg = format!("Model returned {} results for {} posts", results.len(), batch_posts.len());
            error!("{}", err_msg);
            for (del, body) in batch_deliveries {
                let attempt = get_attempt_header(&del);
                let _ = retry_or_dead_letter(&channel, input_queue, &body, del.delivery_tag, attempt, &err_msg).await;
            }
            continue;
        }

        let now = Utc::now();

        for ((clean_post, (delivery, body)), (sentiment_label, scores)) in
            batch_posts.into_iter().zip(batch_deliveries).zip(results)
        {
            let attempt = get_attempt_header(&delivery);
            let scored_post = ScoredPost {
                clean: clean_post,
                sentiment: sentiment_label,
                scores,
                sentiment_scored_at: now,
                sentiment_model_version: sentiment_model.version.clone(),
                sentiment_model_hash: sentiment_model.model_hash.clone(),
            };

            if let Err(val_err) = scored_post.validate() {
                error!("Invalid ScoredPost validation for {}: {}", scored_post.clean.id, val_err);
                let _ = dead_letter_message(&channel, input_queue, &body, delivery.delivery_tag, &val_err).await;
                continue;
            }

            let scored_bytes = match serde_json::to_vec(&scored_post) {
                Ok(b) => b,
                Err(e) => {
                    error!("Failed to serialize scored post {}: {}", scored_post.clean.id, e);
                    let _ = retry_or_dead_letter(&channel, input_queue, &body, delivery.delivery_tag, attempt, &e.to_string()).await;
                    continue;
                }
            };

            let props = BasicProperties::default().with_delivery_mode(2);
            if let Err(e) = channel
                .basic_publish("", output_queue, BasicPublishOptions::default(), &scored_bytes, props)
                .await
            {
                error!("Error publishing scored post {}: {}", scored_post.clean.id, e);
                let _ = retry_or_dead_letter(&channel, input_queue, &body, delivery.delivery_tag, attempt, &e.to_string()).await;
                continue;
            }

            let _ = channel.basic_ack(delivery.delivery_tag, BasicAckOptions::default()).await;
            info!(
                "Scored {}: sentiment='{}', pos={:.4}, neu={:.4}, neg={:.4}",
                scored_post.clean.id,
                scored_post.sentiment,
                scored_post.scores.get("positive").cloned().unwrap_or(0.0),
                scored_post.scores.get("neutral").cloned().unwrap_or(0.0),
                scored_post.scores.get("negative").cloned().unwrap_or(0.0),
            );
        }
    }

    Ok(())
}
