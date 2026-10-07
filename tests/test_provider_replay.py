"""Offline coverage for the provider record/replay shadow-capture server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import build_opener, urlopen

import pytest

from scripts.provider_replay import (
    ProviderReplayServer,
    ReplayStore,
    SessionFormatError,
    SessionRecorder,
    SessionSanitizationError,
    canonical_key,
    load_session,
)


class RunningServer:
    """Run an HTTP server on an ephemeral loopback port for one test."""

    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self) -> ThreadingHTTPServer:
        self.thread.start()
        return self.server

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def url(server: ThreadingHTTPServer, target: str) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{target}"


def record_session(path, provider: str = "bluesky", upstream: str = "https://api.bsky.app"):
    recorder = SessionRecorder(path, provider=provider, upstream=upstream)
    return recorder


def test_canonical_key_absorbs_encoding_and_parameter_order():
    plus = canonical_key(
        "GET", "/xrpc/app.bsky.feed.searchPosts?q=Apple+Inc&limit=25&sort=latest"
    )
    percent = canonical_key(
        "GET", "/xrpc/app.bsky.feed.searchPosts?sort=latest&limit=25&q=Apple%20Inc"
    )
    assert plus == percent


def test_canonical_key_keeps_distinct_requests_distinct():
    first = canonical_key("GET", "/api/2/streams/symbol/AAPL.json?since=123")
    second = canonical_key("GET", "/api/2/streams/symbol/AAPL.json?since=124")
    assert first != second


def test_session_roundtrip_preserves_per_key_order(tmp_path):
    path = tmp_path / "session.jsonl"
    recorder = record_session(path)
    key = canonical_key("GET", "/xrpc/app.bsky.feed.searchPosts?q=AAPL")
    recorder.append(
        key=key, status=200, content_type="application/json", retry_after=None, body=b'{"posts": []}'
    )
    recorder.append(
        key=key, status=200, content_type="application/json", retry_after=None, body=b'{"posts": [1]}'
    )
    recorder.close()

    header, exchanges = load_session(path)
    assert header["provider"] == "bluesky"
    assert [record["body"] for record in exchanges[key]] == ['{"posts": []}', '{"posts": [1]}']


def test_session_refuses_credential_like_query_parameters(tmp_path):
    recorder = record_session(tmp_path / "session.jsonl", provider="reddit")
    with pytest.raises(SessionSanitizationError):
        recorder.append(
            key=canonical_key("GET", "/x?token=abc"),
            status=200,
            content_type=None,
            retry_after=None,
            body=b"{}",
        )
    recorder.close()


def test_load_session_rejects_unsupported_version(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "header", "version": 99, "provider": "bluesky"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="version"):
        load_session(path)


def _replay_store(path, **policy: str) -> ReplayStore:
    header, exchanges = load_session(path)
    return ReplayStore(exchanges, provider=header["provider"], **policy)


def test_replay_serves_recorded_responses_in_order_then_empty(tmp_path):
    path = tmp_path / "session.jsonl"
    recorder = record_session(path)
    key = canonical_key("GET", "/xrpc/app.bsky.feed.searchPosts?q=AAPL")
    recorder.append(
        key=key, status=200, content_type="application/json", retry_after=None, body=b'{"posts": [1]}'
    )
    recorder.append(
        key=key, status=200, content_type="application/json", retry_after=None, body=b'{"posts": [2]}'
    )
    recorder.close()

    server = ProviderReplayServer(
        ("127.0.0.1", 0), mode="replay", store=_replay_store(path)
    )
    with RunningServer(server):
        target = "/xrpc/app.bsky.feed.searchPosts?q=AAPL"
        assert urlopen(url(server, target), timeout=5).read() == b'{"posts": [1]}'
        assert urlopen(url(server, target), timeout=5).read() == b'{"posts": [2]}'
        # Recorded exchanges are exhausted: provider-shaped empty payload.
        assert urlopen(url(server, target), timeout=5).read() == b'{"posts": []}'
        # A request that was never recorded fails loudly by default.
        with pytest.raises(HTTPError) as error:
            urlopen(url(server, "/xrpc/app.bsky.feed.searchPosts?q=UNKNOWN"), timeout=5)
        assert error.value.code == 404


def test_replay_can_answer_unknown_requests_with_empty(tmp_path):
    path = tmp_path / "session.jsonl"
    recorder = record_session(path)
    recorder.close()
    server = ProviderReplayServer(
        ("127.0.0.1", 0),
        mode="replay",
        store=_replay_store(path, on_unknown="empty"),
    )
    with RunningServer(server):
        body = urlopen(url(server, "/xrpc/app.bsky.feed.searchPosts?q=NEW"), timeout=5).read()
        assert body == b'{"posts": []}'


class _EchoUpstream(BaseHTTPRequestHandler):
    calls: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        type(self).calls.append(self.path)
        body = json.dumps({"echo": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        pass


def test_record_mode_forwards_to_upstream_and_writes_replayable_session(tmp_path):
    _EchoUpstream.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _EchoUpstream)
    upstream_host, upstream_port = upstream.server_address[:2]
    upstream_base = f"http://{upstream_host}:{upstream_port}"

    session = tmp_path / "recorded.jsonl"
    recorder = SessionRecorder(session, provider="bluesky", upstream=upstream_base)
    server = ProviderReplayServer(
        ("127.0.0.1", 0),
        mode="record",
        recorder=recorder,
        upstream=upstream_base,
        opener=build_opener(),
    )
    target = "/xrpc/app.bsky.feed.searchPosts?q=Apple+Inc&limit=25"
    with RunningServer(upstream), RunningServer(server):
        response = urlopen(url(server, target), timeout=5).read()
    recorder.close()

    assert json.loads(response) == {"echo": target}
    assert _EchoUpstream.calls == [target]

    header, exchanges = load_session(session)
    assert header["provider"] == "bluesky"
    key = canonical_key("GET", target)
    assert [record["status"] for record in exchanges[key]] == [200]
    assert json.loads(exchanges[key][0]["body"]) == {"echo": target}

    # The recorded session replays without touching the upstream.
    replay = ProviderReplayServer(
        ("127.0.0.1", 0), mode="replay", store=_replay_store(session)
    )
    with RunningServer(replay):
        assert json.loads(urlopen(url(replay, target), timeout=5).read()) == {"echo": target}
