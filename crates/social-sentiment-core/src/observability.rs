use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tracing::error;

static MESSAGES_PROCESSED: AtomicU64 = AtomicU64::new(0);
static PROCESSING_ERRORS: AtomicU64 = AtomicU64::new(0);

pub fn increment_processed(count: u64) {
    MESSAGES_PROCESSED.fetch_add(count, Ordering::Relaxed);
}

pub fn increment_errors(count: u64) {
    PROCESSING_ERRORS.fetch_add(count, Ordering::Relaxed);
}

pub fn metrics_body(service: &str) -> String {
    format!(
        "# HELP messages_processed_total Successfully processed pipeline messages.\n\
         # TYPE messages_processed_total counter\n\
         messages_processed_total{{service=\"{service}\"}} {}\n\
         # HELP message_processing_errors_total Pipeline message processing failures.\n\
         # TYPE message_processing_errors_total counter\n\
         message_processing_errors_total{{service=\"{service}\"}} {}\n",
        MESSAGES_PROCESSED.load(Ordering::Relaxed),
        PROCESSING_ERRORS.load(Ordering::Relaxed),
    )
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
