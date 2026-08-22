//! Shared producer contracts and state which deliberately commit cursors only
//! after a caller has confirmed its RabbitMQ publication or database write.

use std::{
    collections::{HashMap, VecDeque},
    hash::Hash,
    sync::Arc,
    time::Duration,
};

use anyhow::Result;
use async_trait::async_trait;
use lapin::{
    options::{ConfirmSelectOptions, QueueDeclareOptions},
    types::FieldTable,
    BasicProperties, Channel, Connection, ConnectionProperties,
};
use tokio::sync::RwLock;

use crate::{
    messaging::publish_confirmed, repository::Repository, schemas::RawPost, symbols::SymbolConfig,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderStatus {
    Success,
    NoData,
    RateLimited,
    Blocked,
    TransientError,
    PermanentError,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AdapterOutcome<T> {
    pub status: ProviderStatus,
    pub items: Vec<T>,
    pub retry_after: Option<Duration>,
    /// A diagnostic safe to log. Adapters must never put credentials here.
    pub detail: Option<String>,
}

impl<T> AdapterOutcome<T> {
    pub fn success(items: Vec<T>) -> Self {
        Self {
            status: if items.is_empty() {
                ProviderStatus::NoData
            } else {
                ProviderStatus::Success
            },
            items,
            retry_after: None,
            detail: None,
        }
    }

    pub fn failure(status: ProviderStatus, retry_after: Option<Duration>) -> Self {
        Self {
            status,
            items: Vec::new(),
            retry_after,
            detail: None,
        }
    }
}

#[async_trait]
pub trait RawPostPublisher: Send + Sync {
    async fn publish(&self, post: &RawPost) -> Result<()>;
}

pub struct ConfirmedPublisher {
    _connection: Connection,
    channel: Channel,
    queue: String,
}

impl ConfirmedPublisher {
    pub async fn connect(rabbit_url: &str, queue: &str) -> Result<Self> {
        let connection = Connection::connect(rabbit_url, ConnectionProperties::default()).await?;
        let channel = connection.create_channel().await?;
        channel
            .confirm_select(ConfirmSelectOptions::default())
            .await?;
        channel
            .queue_declare(
                queue,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
        Ok(Self {
            _connection: connection,
            channel,
            queue: queue.to_owned(),
        })
    }
}

#[async_trait]
impl RawPostPublisher for ConfirmedPublisher {
    async fn publish(&self, post: &RawPost) -> Result<()> {
        let payload = serde_json::to_vec(post)?;
        let properties = BasicProperties::default()
            .with_delivery_mode(2)
            .with_content_type("application/json".into());
        publish_confirmed(&self.channel, &self.queue, &payload, properties).await
    }
}

/// Deterministic, bounded insertion-order map used for cursors and dedup IDs.
#[derive(Debug, Clone)]
pub struct BoundedCursor<K, V> {
    capacity: usize,
    values: HashMap<K, V>,
    order: VecDeque<K>,
}

impl<K, V> BoundedCursor<K, V>
where
    K: Clone + Eq + Hash,
{
    pub fn new(capacity: usize) -> Self {
        assert!(capacity > 0, "cursor capacity must be positive");
        Self {
            capacity,
            values: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    pub fn get(&self, key: &K) -> Option<&V> {
        self.values.get(key)
    }

    pub fn contains_key(&self, key: &K) -> bool {
        self.values.contains_key(key)
    }

    pub fn commit(&mut self, key: K, value: V) {
        if self.values.contains_key(&key) {
            self.order.retain(|existing| existing != &key);
        }
        self.order.push_back(key.clone());
        self.values.insert(key, value);
        while self.values.len() > self.capacity {
            if let Some(expired) = self.order.pop_front() {
                self.values.remove(&expired);
            }
        }
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
}

/// Process-local tracked-symbol snapshot. Failed refreshes retain the prior
/// snapshot, while a successful empty query intentionally clears it.
#[derive(Clone)]
pub struct TrackedSymbolRegistry {
    repository: Repository,
    snapshot: Arc<RwLock<Vec<SymbolConfig>>>,
}

impl TrackedSymbolRegistry {
    pub fn new(repository: Repository) -> Self {
        Self {
            repository,
            snapshot: Arc::new(RwLock::new(Vec::new())),
        }
    }

    pub async fn refresh(&self) -> Result<()> {
        let rows = self.repository.active_symbols().await?;
        let symbols = rows
            .into_iter()
            .map(|row| SymbolConfig {
                symbol: row.symbol,
                keywords: row.keywords,
                future: row.future,
                sector: row.sector,
                require_uppercase: row.require_uppercase,
                block_phrases: row.block_phrases,
                require_cashtag: row.require_cashtag,
            })
            .collect();
        *self.snapshot.write().await = symbols;
        Ok(())
    }

    pub async fn snapshot(&self) -> Vec<SymbolConfig> {
        self.snapshot.read().await.clone()
    }

    pub fn spawn_refresh_loop(&self, interval: Duration) -> tokio::task::JoinHandle<()> {
        let registry = self.clone();
        tokio::spawn(async move {
            let mut retry = Duration::from_secs(5);
            let mut delay = interval;
            loop {
                tokio::time::sleep(delay).await;
                delay = match registry.refresh().await {
                    Ok(()) => {
                        retry = Duration::from_secs(5);
                        interval
                    }
                    Err(error) => {
                        tracing::warn!(%error, "tracked-symbol refresh failed; retaining snapshot");
                        let current = retry;
                        retry = (retry * 2).min(Duration::from_secs(60));
                        current
                    }
                };
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cursor_is_bounded_and_updates_recency() {
        let mut cursor = BoundedCursor::new(2);
        cursor.commit("a", 1);
        cursor.commit("b", 2);
        cursor.commit("a", 3);
        cursor.commit("c", 4);
        assert_eq!(cursor.get(&"a"), Some(&3));
        assert!(!cursor.contains_key(&"b"));
        assert_eq!(cursor.len(), 2);
    }
}
