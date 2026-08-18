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
    if err_str.len() > max_len {
        err_str[..max_len].to_string()
    } else {
        err_str.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_dead_letter_queue_name() {
        assert_eq!(dead_letter_queue_name("raw-posts"), "raw-posts.dead-letter");
        assert_eq!(dead_letter_queue_name("clean-posts"), "clean-posts.dead-letter");
        assert_eq!(dead_letter_queue_name("scored-posts"), "scored-posts.dead-letter");
    }

    #[test]
    fn test_truncate_error() {
        let long_err = "x".repeat(1000);
        let truncated = truncate_error(&long_err, 500);
        assert_eq!(truncated.len(), 500);
    }
}
