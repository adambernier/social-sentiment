use anyhow::{bail, Result};
use lapin::{
    options::BasicPublishOptions, publisher_confirm::Confirmation, BasicProperties, Channel,
};

pub const PROCESSING_ATTEMPT_HEADER: &str = "x-processing-attempt";
pub const ORIGINAL_QUEUE_HEADER: &str = "x-original-queue";
pub const ERROR_TYPE_HEADER: &str = "x-error-type";
pub const ERROR_HEADER: &str = "x-error";
pub const LAST_ERROR_TYPE_HEADER: &str = "x-last-error-type";
pub const LAST_ERROR_HEADER: &str = "x-last-error";

pub fn dead_letter_queue_name(input_queue: &str) -> String {
    format!("{}.dead-letter", input_queue)
}

pub fn truncate_error(err_str: &str, max_len: usize) -> String {
    err_str.chars().take(max_len).collect()
}

/// Publish a persistent pipeline message and wait for RabbitMQ to confirm it.
/// Callers may acknowledge the input delivery only after this succeeds.
pub async fn publish_confirmed(
    channel: &Channel,
    routing_key: &str,
    payload: &[u8],
    properties: BasicProperties,
) -> Result<()> {
    let confirmation = channel
        .basic_publish(
            "",
            routing_key,
            BasicPublishOptions {
                mandatory: true,
                ..Default::default()
            },
            payload,
            properties,
        )
        .await?
        .await?;

    match confirmation {
        Confirmation::Ack(None) => Ok(()),
        Confirmation::Ack(Some(returned)) => {
            bail!("RabbitMQ returned published message: {returned:?}")
        }
        Confirmation::Nack(_) => bail!("RabbitMQ negatively acknowledged published message"),
        Confirmation::NotRequested => bail!("RabbitMQ publisher confirms are not enabled"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schemas::RawPost;
    use serde::Deserialize;
    use serde_json::Value;
    use std::collections::HashMap;

    #[derive(Deserialize)]
    struct ReplayFixture {
        scenario: String,
        queue: String,
        payload: Value,
        headers: HashMap<String, Value>,
        expected: String,
    }

    #[test]
    fn test_dead_letter_queue_name() {
        assert_eq!(dead_letter_queue_name("raw-posts"), "raw-posts.dead-letter");
        assert_eq!(
            dead_letter_queue_name("clean-posts"),
            "clean-posts.dead-letter"
        );
        assert_eq!(
            dead_letter_queue_name("scored-posts"),
            "scored-posts.dead-letter"
        );
    }

    #[test]
    fn test_truncate_error() {
        let long_err = "x".repeat(1000);
        let truncated = truncate_error(&long_err, 500);
        assert_eq!(truncated.len(), 500);

        let unicode = truncate_error("database failed 💥💥", 17);
        assert_eq!(unicode, "database failed 💥");
    }

    #[test]
    fn replay_fixture_covers_delivery_lifecycle_contract() {
        let fixtures: Vec<ReplayFixture> =
            serde_json::from_str(include_str!("../../../tests/fixtures/rabbitmq_replay.json"))
                .expect("replay fixture");
        let scenarios = fixtures
            .iter()
            .map(|fixture| fixture.scenario.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            scenarios,
            [
                "valid",
                "duplicate",
                "malformed",
                "retried",
                "dead-lettered",
                "interrupted"
            ]
        );
        for fixture in &fixtures {
            assert_eq!(fixture.queue, "raw-posts");
            assert!(!fixture.expected.is_empty());
            if fixture.scenario == "malformed" {
                assert!(serde_json::from_value::<RawPost>(fixture.payload.clone()).is_err());
            } else {
                serde_json::from_value::<RawPost>(fixture.payload.clone())
                    .expect("valid raw-post fixture");
            }
        }
        assert_eq!(
            fixtures[3].headers[PROCESSING_ATTEMPT_HEADER],
            Value::from(0)
        );
        assert_eq!(
            fixtures[4].headers[PROCESSING_ATTEMPT_HEADER],
            Value::from(1)
        );
        assert!(fixtures[5].headers.is_empty());
    }
}
