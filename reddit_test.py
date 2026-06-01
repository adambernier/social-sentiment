#!/usr/bin/env python3
"""Ad-hoc probe: can we read Reddit's unauthenticated .json feed through the proxy?

Reads REDDIT_PROXY_URL + REDDIT_USER_AGENT from the environment (the same vars
the reddit-producer uses). Each run appends a fresh ``_session-XXXXXXXX`` suffix
to the proxy password so iProyal routes through a new egress IP, prints that IP
(to confirm the rotation took), then fetches a couple of comments and reports
whether Reddit served them (200) or blocked us (403).

Usage:
    REDDIT_PROXY_URL="user:pass@geo.iproyal.com:12321" .venv/bin/python reddit_probe.py

Re-run to rotate the IP. Exits 0 if Reddit returned comments, 1 if blocked/error.
"""
import os
import random
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

import httpx

IP_ECHO_URL = "https://ipv4.icanhazip.com"
FEED_URL = "https://www.reddit.com/r/wallstreetbets/comments.json?limit=2"
DEFAULT_UA = "social-sentiment/1.0 (contact: adam.c.bernier@gmail.com)"

BROWSER_PROFILES = [
    {
        "name": "Chrome on Windows",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    },
    {
        "name": "Firefox on Windows",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    },
    {
        "name": "Chrome on macOS",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    },
    {
        "name": "Safari on macOS",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    }
]


def with_session(proxy_url: str) -> tuple[str, str]:
    """Return (rotated_proxy_url, session_tag), adding a fresh session suffix to the password."""
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = urlsplit(proxy_url)
    session_tag = f"_session-{secrets.token_hex(4)}"
    password = (parts.password or "") + session_tag
    userinfo = parts.username or ""
    if password:
        userinfo = f"{userinfo}:{password}"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{userinfo}@{host}" if userinfo else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), session_tag


def main() -> int:
    proxy_url = os.environ.get("REDDIT_PROXY_URL")
    if not proxy_url:
        print("REDDIT_PROXY_URL is not set. Export it and re-run.", file=sys.stderr)
        return 2

    # Choose a random browser profile for fingerprint variation
    profile = random.choice(BROWSER_PROFILES)
    headers = profile["headers"].copy()

    # If the user explicitly provided REDDIT_USER_AGENT in env, honor it
    env_ua = os.environ.get("REDDIT_USER_AGENT")
    if env_ua:
        headers["User-Agent"] = env_ua
        profile_name = f"Custom UA (via env)"
    else:
        profile_name = profile["name"]

    rotated, session_tag = with_session(proxy_url)
    # Only the session tag is printed, never the full rotated URL, so the base
    # proxy credentials don't leak into the terminal / shell history.
    print(f"Using fresh proxy session {session_tag} with fingerprint: {profile_name}")
    print(f"User-Agent: {headers['User-Agent']}")

    with httpx.Client(proxy=rotated, headers=headers, timeout=15) as client:
        # 1. Confirm the rotation actually changed our egress IP.
        try:
            ip = client.get(IP_ECHO_URL).text.strip()
            print(f"Egress IP via proxy: {ip}")
        except Exception as e:
            print(f"Could not reach IP echo through proxy: {e}", file=sys.stderr)
            return 1

        # 2. The real test: can we read Reddit's unauthenticated JSON feed?
        try:
            resp = client.get(FEED_URL)
        except Exception as e:
            print(f"Request to Reddit failed: {e}", file=sys.stderr)
            return 1

    print(f"Reddit {FEED_URL} -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"Blocked / error. Body (first 300 chars):\n{resp.text.strip()[:300]}")
        return 1

    children = resp.json().get("data", {}).get("children", [])
    if not children:
        print("200 OK but no comments returned.")
        return 0
    print(f"Success -- got {len(children)} comment(s):")
    for child in children[:2]:
        c = child.get("data", {})
        body = (c.get("body") or "").strip().replace("\n", " ")
        print(f"  - r/{c.get('subreddit', '?')} [{c.get('id', '?')}]: {body[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

