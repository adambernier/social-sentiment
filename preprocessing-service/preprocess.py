import html
import re

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
USERNAME_RE = re.compile(r"@\w+")
WHITESPACE_RE = re.compile(r"\s+")

MIN_LENGTH = 3


def clean_text(raw: str) -> str:
    text = html.unescape(raw)
    text = URL_RE.sub("http", text)
    text = USERNAME_RE.sub("@user", text)
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def is_valid(text: str) -> bool:
    return len(text) >= MIN_LENGTH

# Trivial comment to trigger watcher test run
