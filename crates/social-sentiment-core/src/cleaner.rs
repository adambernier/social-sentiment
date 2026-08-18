use once_cell::sync::Lazy;
use regex::Regex;

static URL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)https?://\S+|www\.\S+").expect("valid url regex")
});

static USERNAME_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"@\w+").expect("valid username regex")
});

static WHITESPACE_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\s+").expect("valid whitespace regex")
});

pub const MIN_LENGTH: usize = 3;

pub fn clean_text(raw: &str) -> String {
    let unescaped = html_escape::decode_html_entities(raw);
    let no_urls = URL_RE.replace_all(&unescaped, "http");
    let no_users = USERNAME_RE.replace_all(&no_urls, "@user");
    let clean_ws = WHITESPACE_RE.replace_all(&no_users, " ");
    clean_ws.trim().to_string()
}

pub fn is_valid(text: &str) -> bool {
    text.len() >= MIN_LENGTH
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_clean_text_html_urls_usernames() {
        let raw = "Check out &quot;NVDA&quot; at https://example.com/chart! CC @trader_joe &amp; @alex";
        let cleaned = clean_text(raw);
        assert_eq!(cleaned, "Check out \"NVDA\" at http CC @user & @user");
    }

    #[test]
    fn test_clean_text_whitespace() {
        let raw = "  Multiple    spaces   \n and  newlines  ";
        let cleaned = clean_text(raw);
        assert_eq!(cleaned, "Multiple spaces and newlines");
    }

    #[test]
    fn test_is_valid() {
        assert!(is_valid("AAPL"));
        assert!(is_valid("123"));
        assert!(!is_valid("a"));
        assert!(!is_valid("hi"));
    }
}
