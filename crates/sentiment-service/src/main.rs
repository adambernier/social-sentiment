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
use service_sentiment::sentiment_model::SentimentModel;
use social_sentiment_core::{
    config::Config,
    messaging::{
        dead_letter_queue_name, publish_confirmed, truncate_error, ERROR_HEADER, ERROR_TYPE_HEADER,
        LAST_ERROR_HEADER, LAST_ERROR_TYPE_HEADER, ORIGINAL_QUEUE_HEADER,
        PROCESSING_ATTEMPT_HEADER,
    },
    observability::{increment_errors, increment_processed, start_metrics_server},
    runtime::shutdown_signal,
    schemas::{CleanPost, ScoredPost},
};
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};

const BATCH_SIZE: usize = 32;
const BATCH_TIMEOUT: Duration = Duration::from_secs(1);

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
    delivery: &lapin::message::Delivery,
    error_message: &str,
) -> Result<()> {
    let mut headers = delivery_headers(delivery);
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
        .basic_ack(delivery.delivery_tag, BasicAckOptions::default())
        .await?;
    increment_errors(1);
    Ok(())
}

async fn retry_or_dead_letter(
    channel: &Channel,
    input_queue: &str,
    payload: &[u8],
    delivery: &lapin::message::Delivery,
    attempt: i32,
    error_message: &str,
) -> Result<()> {
    if attempt >= 1 {
        return dead_letter_message(channel, input_queue, payload, delivery, error_message).await;
    }

    let mut headers = delivery_headers(delivery);
    headers.insert(
        PROCESSING_ATTEMPT_HEADER.into(),
        AMQPValue::LongInt(attempt + 1),
    );
    headers.insert(
        LAST_ERROR_TYPE_HEADER.into(),
        AMQPValue::LongString("ProcessingError".into()),
    );
    headers.insert(
        LAST_ERROR_HEADER.into(),
        AMQPValue::LongString(truncate_error(error_message, 500).into()),
    );
    let properties = BasicProperties::default()
        .with_delivery_mode(2)
        .with_content_type("application/json".into())
        .with_headers(headers);
    publish_confirmed(channel, input_queue, payload, properties).await?;
    channel
        .basic_ack(delivery.delivery_tag, BasicAckOptions::default())
        .await?;
    increment_errors(1);
    Ok(())
}

fn delivery_headers(delivery: &lapin::message::Delivery) -> FieldTable {
    delivery
        .properties
        .headers()
        .as_ref()
        .cloned()
        .unwrap_or_default()
}

async fn process_batch(
    posts: Vec<CleanPost>,
    deliveries: Vec<(lapin::message::Delivery, Vec<u8>)>,
    model: Arc<SentimentModel>,
    channel: &Channel,
    input_queue: &str,
    output_queue: &str,
) -> Result<()> {
    let texts: Vec<String> = posts.iter().map(|post| post.text.clone()).collect();
    let inference_model = Arc::clone(&model);
    let results =
        match tokio::task::spawn_blocking(move || inference_model.predict_batch(&texts)).await {
            Ok(Ok(results)) if results.len() == posts.len() => results,
            Ok(Ok(results)) => {
                let error_message = format!(
                    "model returned {} results for {} posts",
                    results.len(),
                    posts.len()
                );
                for (delivery, payload) in deliveries {
                    retry_or_dead_letter(
                        channel,
                        input_queue,
                        &payload,
                        &delivery,
                        get_attempt_header(&delivery),
                        &error_message,
                    )
                    .await?;
                }
                return Ok(());
            }
            Ok(Err(err)) => {
                for (delivery, payload) in deliveries {
                    retry_or_dead_letter(
                        channel,
                        input_queue,
                        &payload,
                        &delivery,
                        get_attempt_header(&delivery),
                        &err.to_string(),
                    )
                    .await?;
                }
                return Ok(());
            }
            Err(err) => {
                for (delivery, payload) in deliveries {
                    retry_or_dead_letter(
                        channel,
                        input_queue,
                        &payload,
                        &delivery,
                        get_attempt_header(&delivery),
                        &err.to_string(),
                    )
                    .await?;
                }
                return Ok(());
            }
        };

    for ((clean_post, (delivery, input_payload)), (sentiment, scores)) in
        posts.into_iter().zip(deliveries).zip(results)
    {
        let scored_post = ScoredPost {
            clean: clean_post,
            sentiment,
            scores,
            sentiment_scored_at: Utc::now(),
            sentiment_model_version: model.version.clone(),
            sentiment_model_hash: model.model_hash.clone(),
        };
        if let Err(validation_error) = scored_post.validate() {
            dead_letter_message(
                channel,
                input_queue,
                &input_payload,
                &delivery,
                &validation_error,
            )
            .await?;
            continue;
        }

        let output_payload = serde_json::to_vec(&scored_post)?;
        let properties = BasicProperties::default()
            .with_delivery_mode(2)
            .with_content_type("application/json".into());
        if let Err(err) =
            publish_confirmed(channel, output_queue, &output_payload, properties).await
        {
            retry_or_dead_letter(
                channel,
                input_queue,
                &input_payload,
                &delivery,
                get_attempt_header(&delivery),
                &err.to_string(),
            )
            .await?;
            continue;
        }
        channel
            .basic_ack(delivery.delivery_tag, BasicAckOptions::default())
            .await?;
        increment_processed(1);
    }
    Ok(())
}

async fn run_consumer_session(config: &Config, model: Arc<SentimentModel>) -> Result<()> {
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

    let input_queue = &config.queue_clean_posts;
    let output_queue = &config.queue_scored_posts;
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
            "sentiment-consumer",
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
                match serde_json::from_slice::<CleanPost>(&payload) {
                    Ok(post) => {
                        deliveries.push((delivery, payload));
                        posts.push(post);
                        if posts.len() < BATCH_SIZE {
                            continue;
                        }
                    }
                    Err(err) => {
                        dead_letter_message(
                            &channel,
                            input_queue,
                            &payload,
                            &delivery,
                            &err.to_string(),
                        ).await?;
                        continue;
                    }
                }
            }
            () = &mut timeout => {}
        }

        if !posts.is_empty() {
            process_batch(
                std::mem::take(&mut posts),
                std::mem::take(&mut deliveries),
                Arc::clone(&model),
                &channel,
                input_queue,
                output_queue,
            )
            .await?;
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    start_metrics_server(8009, "sentiment");

    let config = Config::from_env();
    let model_dir =
        std::env::var("MODEL_DIR").unwrap_or_else(|_| "sentiment-service/model_quant".to_string());
    let model = Arc::new(SentimentModel::new(&model_dir)?);
    info!(model_hash = %model.model_hash, "sentiment model loaded");

    loop {
        tokio::select! {
            result = run_consumer_session(&config, Arc::clone(&model)) => {
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
