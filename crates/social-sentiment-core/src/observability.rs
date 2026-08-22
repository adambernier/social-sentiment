use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        LazyLock, Mutex,
    },
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tracing::error;

static MESSAGES_PROCESSED: AtomicU64 = AtomicU64::new(0);
static PROCESSING_ERRORS: AtomicU64 = AtomicU64::new(0);
static POSTS_INGESTED: LazyLock<Mutex<HashMap<(String, String), u64>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static PROVIDER_REQUESTS: LazyLock<Mutex<HashMap<(String, String), u64>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static RATE_LIMITS: LazyLock<Mutex<HashMap<String, u64>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
type GlobalProviderKey = (String, String, String);
static GLOBAL_PROVIDER_REQUESTS: LazyLock<Mutex<HashMap<GlobalProviderKey, u64>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static GLOBAL_LAST_SUCCESS: LazyLock<Mutex<HashMap<(String, String), f64>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static GLOBAL_RULE_MATCHES: LazyLock<Mutex<HashMap<(String, String), u64>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

pub fn increment_processed(count: u64) {
    MESSAGES_PROCESSED.fetch_add(count, Ordering::Relaxed);
}

pub fn increment_errors(count: u64) {
    PROCESSING_ERRORS.fetch_add(count, Ordering::Relaxed);
}

pub fn increment_ingested(platform: &str, symbol: &str, count: u64) {
    *POSTS_INGESTED
        .lock()
        .expect("producer metrics lock")
        .entry((platform.to_owned(), symbol.to_owned()))
        .or_default() += count;
}

pub fn increment_provider_request(provider: &str, status: &str) {
    *PROVIDER_REQUESTS
        .lock()
        .expect("provider metrics lock")
        .entry((provider.to_owned(), status.to_owned()))
        .or_default() += 1;
}

pub fn increment_rate_limit(platform: &str) {
    *RATE_LIMITS
        .lock()
        .expect("rate-limit metrics lock")
        .entry(platform.to_owned())
        .or_default() += 1;
}

pub fn initialize_rate_limit(platform: &str) {
    RATE_LIMITS
        .lock()
        .expect("rate-limit metrics lock")
        .entry(platform.to_owned())
        .or_default();
}

pub fn increment_global_provider_request(provider: &str, data_type: &str, status: &str) {
    *GLOBAL_PROVIDER_REQUESTS
        .lock()
        .expect("global provider metrics lock")
        .entry((provider.to_owned(), data_type.to_owned(), status.to_owned()))
        .or_default() += 1;
}

pub fn set_global_last_success(provider: &str, data_type: &str, timestamp: f64) {
    GLOBAL_LAST_SUCCESS
        .lock()
        .expect("global success metrics lock")
        .insert((provider.to_owned(), data_type.to_owned()), timestamp);
}

pub fn increment_global_rule_matches(provider: &str, symbol: &str, count: u64) {
    *GLOBAL_RULE_MATCHES
        .lock()
        .expect("global rule metrics lock")
        .entry((provider.to_owned(), symbol.to_owned()))
        .or_default() += count;
}

fn producer_metrics_body() -> String {
    let mut body = String::from(
        "# HELP posts_ingested_total Total number of posts/messages ingested.\n\
         # TYPE posts_ingested_total counter\n",
    );
    let posts = POSTS_INGESTED.lock().expect("producer metrics lock");
    let mut rows = posts.iter().collect::<Vec<_>>();
    rows.sort_by_key(|((platform, symbol), _)| (platform.as_str(), symbol.as_str()));
    for ((platform, symbol), count) in rows {
        body.push_str(&format!(
            "posts_ingested_total{{platform=\"{platform}\",symbol=\"{symbol}\"}} {count}\n"
        ));
    }
    drop(posts);

    body.push_str(
        "# HELP provider_requests_total Provider requests by normalized outcome.\n\
         # TYPE provider_requests_total counter\n",
    );
    let requests = PROVIDER_REQUESTS.lock().expect("provider metrics lock");
    let mut rows = requests.iter().collect::<Vec<_>>();
    rows.sort_by_key(|((provider, status), _)| (provider.as_str(), status.as_str()));
    for ((provider, status), count) in rows {
        body.push_str(&format!(
            "provider_requests_total{{provider=\"{provider}\",status=\"{status}\"}} {count}\n"
        ));
    }
    drop(requests);

    body.push_str(
        "# HELP rate_limits_hit_total Total number of API rate limits encountered.\n\
         # TYPE rate_limits_hit_total counter\n",
    );
    let limits = RATE_LIMITS.lock().expect("rate-limit metrics lock");
    let mut rows = limits.iter().collect::<Vec<_>>();
    rows.sort_by_key(|(platform, _)| platform.as_str());
    for (platform, count) in rows {
        body.push_str(&format!(
            "rate_limits_hit_total{{platform=\"{platform}\"}} {count}\n"
        ));
    }
    drop(limits);

    body.push_str(
        "# HELP global_context_provider_requests_total Global-context provider requests by type and result.\n\
         # TYPE global_context_provider_requests_total counter\n",
    );
    let requests = GLOBAL_PROVIDER_REQUESTS
        .lock()
        .expect("global provider metrics lock");
    let mut rows = requests.iter().collect::<Vec<_>>();
    rows.sort_by_key(|((provider, data_type, status), _)| {
        (provider.as_str(), data_type.as_str(), status.as_str())
    });
    for ((provider, data_type, status), count) in rows {
        body.push_str(&format!(
            "global_context_provider_requests_total{{provider=\"{provider}\",data_type=\"{data_type}\",status=\"{status}\"}} {count}\n"
        ));
    }
    drop(requests);

    body.push_str(
        "# HELP global_context_last_success_timestamp_seconds Unix timestamp of the latest successful global-context ingestion.\n\
         # TYPE global_context_last_success_timestamp_seconds gauge\n",
    );
    let successes = GLOBAL_LAST_SUCCESS
        .lock()
        .expect("global success metrics lock");
    let mut rows = successes.iter().collect::<Vec<_>>();
    rows.sort_by_key(|((provider, data_type), _)| (provider.as_str(), data_type.as_str()));
    for ((provider, data_type), timestamp) in rows {
        body.push_str(&format!(
            "global_context_last_success_timestamp_seconds{{provider=\"{provider}\",data_type=\"{data_type}\"}} {timestamp}\n"
        ));
    }
    drop(successes);

    body.push_str(
        "# HELP global_context_rule_matches_total Events linked by an explicit global-context rule.\n\
         # TYPE global_context_rule_matches_total counter\n",
    );
    let matches = GLOBAL_RULE_MATCHES
        .lock()
        .expect("global rule metrics lock");
    let mut rows = matches.iter().collect::<Vec<_>>();
    rows.sort_by_key(|((provider, symbol), _)| (provider.as_str(), symbol.as_str()));
    for ((provider, symbol), count) in rows {
        body.push_str(&format!(
            "global_context_rule_matches_total{{provider=\"{provider}\",symbol=\"{symbol}\"}} {count}\n"
        ));
    }
    body
}

pub fn metrics_body(service: &str) -> String {
    let mut body = format!(
        "# HELP messages_processed_total Successfully processed pipeline messages.\n\
         # TYPE messages_processed_total counter\n\
         messages_processed_total{{service=\"{service}\"}} {}\n\
         # HELP message_processing_errors_total Pipeline message processing failures.\n\
         # TYPE message_processing_errors_total counter\n\
         message_processing_errors_total{{service=\"{service}\"}} {}\n",
        MESSAGES_PROCESSED.load(Ordering::Relaxed),
        PROCESSING_ERRORS.load(Ordering::Relaxed),
    );
    body.push_str(&producer_metrics_body());
    body
}

pub fn start_metrics_server(port: u16, service: &'static str) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let listener = match TcpListener::bind(("0.0.0.0", port)).await {
            Ok(listener) => listener,
            Err(err) => {
                error!(port, %err, "failed to bind metrics listener");
                return;
            }
        };

        loop {
            let (mut stream, _) = match listener.accept().await {
                Ok(connection) => connection,
                Err(err) => {
                    error!(%err, "metrics listener accept failed");
                    continue;
                }
            };
            tokio::spawn(async move {
                let mut request = [0_u8; 1024];
                let _ = stream.read(&mut request).await;
                let body = metrics_body(service);
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body,
                );
                let _ = stream.write_all(response.as_bytes()).await;
            });
        }
    })
}
