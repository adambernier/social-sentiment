use std::env;

#[derive(Debug, Clone)]
pub struct Config {
    pub rabbit_host: String,
    pub rabbit_port: u16,
    pub rabbit_user: String,
    pub rabbit_pass: String,
    pub queue_raw_posts: String,
    pub queue_clean_posts: String,
    pub queue_scored_posts: String,
    pub queue_topic_posts: String,
    pub database_dsn: String,
    pub post_retention_days: i32,
    pub quote_retention_days: i32,
    pub raw_archive_platforms: Vec<String>,
    pub raw_archive_sample_rate: f64,
    pub raw_archive_challenge_engagement: i32,
    pub raw_archive_challenge_abs_signal: f64,
    pub global_context_enabled: bool,
    pub vix_symbol: String,
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            rabbit_host: get_env("RABBITMQ_HOST", "localhost"),
            rabbit_port: get_env_u16("RABBITMQ_PORT", 5672),
            rabbit_user: get_env("RABBITMQ_USER", "guest"),
            rabbit_pass: get_env("RABBITMQ_PASS", "guest"),
            queue_raw_posts: get_env("QUEUE_RAW_POSTS", "raw-posts"),
            queue_clean_posts: get_env("QUEUE_CLEAN_POSTS", "clean-posts"),
            queue_scored_posts: get_env("QUEUE_SCORED_POSTS", "scored-posts"),
            queue_topic_posts: get_env("QUEUE_TOPIC_POSTS", "topic-posts"),
            database_dsn: get_env(
                "DATABASE_DSN",
                "postgresql://postgres:sentiment@localhost:5432/sentiment",
            ),
            post_retention_days: get_env_i32("POST_RETENTION_DAYS", 14).max(1),
            quote_retention_days: get_env_i32("QUOTE_RETENTION_DAYS", 90).max(1),
            raw_archive_platforms: get_env_csv("RAW_ARCHIVE_PLATFORMS"),
            raw_archive_sample_rate: get_env_f64("RAW_ARCHIVE_SAMPLE_RATE", 0.01).clamp(0.0, 1.0),
            raw_archive_challenge_engagement: get_env_i32("RAW_ARCHIVE_CHALLENGE_ENGAGEMENT", 100)
                .max(0),
            raw_archive_challenge_abs_signal: get_env_f64("RAW_ARCHIVE_CHALLENGE_ABS_SIGNAL", 0.8)
                .clamp(0.0, 1.0),
            global_context_enabled: get_env_bool("GLOBAL_CONTEXT_ENABLED", false),
            vix_symbol: get_env("VIX_SYMBOL", "^VIX"),
        }
    }

    pub fn rabbit_url(&self) -> String {
        format!(
            "amqp://{}:{}@{}:{}/%2f",
            self.rabbit_user, self.rabbit_pass, self.rabbit_host, self.rabbit_port
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OrtThreadConfig {
    pub intra_threads: usize,
    pub inter_threads: usize,
}

impl OrtThreadConfig {
    /// Resolve ONNX Runtime thread pool sizes from `ORT_INTRA_THREADS` and
    /// `ORT_INTER_THREADS`. Defaults cap intra-op threads to `min(available_cpus, 2)`
    /// and inter-op threads to `1` so co-located inference containers do not
    /// oversubscribe shared VM cores.
    pub fn from_env() -> Self {
        let available_cpus = std::thread::available_parallelism()
            .map(|count| count.get())
            .unwrap_or(1);
        let intra_raw = env::var("ORT_INTRA_THREADS").ok();
        let inter_raw = env::var("ORT_INTER_THREADS").ok();
        Self::resolve(intra_raw.as_deref(), inter_raw.as_deref(), available_cpus)
    }

    pub fn resolve(
        intra_raw: Option<&str>,
        inter_raw: Option<&str>,
        available_cpus: usize,
    ) -> Self {
        let default_intra = available_cpus.clamp(1, 2);
        let parse_positive = |raw: Option<&str>| {
            raw.and_then(|value| value.trim().parse::<usize>().ok())
                .filter(|&count| count > 0)
        };
        Self {
            intra_threads: parse_positive(intra_raw).unwrap_or(default_intra),
            inter_threads: parse_positive(inter_raw).unwrap_or(1),
        }
    }
}

fn get_env(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

fn get_env_u16(key: &str, default: u16) -> u16 {
    env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn get_env_i32(key: &str, default: i32) -> i32 {
    env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn get_env_f64(key: &str, default: f64) -> f64 {
    env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn get_env_bool(key: &str, default: bool) -> bool {
    env::var(key)
        .map(|v| match v.trim().to_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => default,
        })
        .unwrap_or(default)
}

fn get_env_csv(key: &str) -> Vec<String> {
    env::var(key)
        .map(|v| {
            v.split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_config_defaults() {
        let config = Config::from_env();
        assert_eq!(config.rabbit_host, "localhost");
        assert_eq!(config.rabbit_port, 5672);
        assert_eq!(config.queue_raw_posts, "raw-posts");
        assert_eq!(config.post_retention_days, 14);
        assert_eq!(config.rabbit_url(), "amqp://guest:guest@localhost:5672/%2f");
    }

    #[test]
    fn test_ort_thread_config_resolution() {
        assert_eq!(
            OrtThreadConfig::resolve(None, None, 8),
            OrtThreadConfig {
                intra_threads: 2,
                inter_threads: 1,
            }
        );
        assert_eq!(
            OrtThreadConfig::resolve(None, None, 1),
            OrtThreadConfig {
                intra_threads: 1,
                inter_threads: 1,
            }
        );
        assert_eq!(
            OrtThreadConfig::resolve(Some("4"), Some("2"), 8),
            OrtThreadConfig {
                intra_threads: 4,
                inter_threads: 2,
            }
        );
        assert_eq!(
            OrtThreadConfig::resolve(Some("0"), Some("invalid"), 4),
            OrtThreadConfig {
                intra_threads: 2,
                inter_threads: 1,
            }
        );
    }
}
