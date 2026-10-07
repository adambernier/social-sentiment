"""Record and replay provider HTTP sessions for producer shadow captures.

The producer shadow-parity comparator (``scripts/verify_producer_parity.py``)
requires both runtimes to publish the *same* provider records: identical
identity keys, identical payloads, and no duplicates. Two independent live runs
cannot guarantee that for the search-feed producers -- Bluesky, StockTwits, and
Reddit poll mutable, cursor-driven feeds, and engagement counters drift between
fetches. This tool closes that gap:

1. ``record`` runs in front of a producer runtime: it forwards each provider
   request to the live upstream and appends every request/response exchange to
   a session file, so the session is exactly the request sequence that runtime
   made.
2. ``replay`` serves a recorded session byte-for-byte with no provider access.
   Run one replay server per runtime (each starts from the beginning of the
   session), point the runtime's provider base URL at it -- see the
   ``*_API_BASE`` / ``REDDIT_FEED_URL`` overrides -- and both runtimes ingest
   identical provider payloads.

Request keys are canonicalized (sorted query parameters, decoded
percent-encoding) so equivalent requests from the two runtimes match the same
recorded exchange. Each key is served in recorded order; once a key's recorded
exchanges run out the server answers with a provider-shaped "no new data"
payload by default, and a request for a key that was never recorded fails
loudly instead of passing silently.

Sessions are JSONL: one header line, then one exchange per line. Query
parameters whose names look like credentials are rejected at record time so a
session cannot silently carry secrets. Keep sessions under ``artifacts/``
(gitignored); they are local capture inputs, not provider fixtures.

Record mode is read-only against the provider and writes only the session
file. Replay mode touches no network at all.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import signal
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener

LOG = logging.getLogger("provider-replay")

SESSION_VERSION = 1
DEFAULT_PORT = 8611
UPSTREAM_TIMEOUT_SECONDS = 30.0

PROVIDER_UPSTREAMS = {
    "bluesky": "https://api.bsky.app",
    "stocktwits": "https://api.stocktwits.com",
    "reddit": "https://www.reddit.com",
}

# Provider-shaped "no new data" payloads the consuming runtimes already accept
# as a successful empty result.
PROVIDER_EMPTY_BODIES = {
    "bluesky": '{"posts": []}',
    "stocktwits": '{"messages": []}',
    "reddit": '{"data": {"children": []}}',
}

SECRET_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_id",
        "client_secret",
        "key",
        "password",
        "secret",
        "token",
    }
)


class SessionError(RuntimeError):
    """Base class for provider session problems."""


class SessionFormatError(SessionError):
    """A session file is malformed or of an unsupported version."""


class SessionSanitizationError(SessionError):
    """A session exchange would carry credential-like query parameters."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_key(method: str, target: str) -> str:
    """Canonicalize a request target so equivalent requests share one key.

    Query parameters are sorted by name and re-encoded after decoding, which
    absorbs parameter-order differences and ``+`` versus ``%20`` spelling
    between the two runtimes. The path is kept verbatim.
    """
    split = urlsplit(target)
    pairs = sorted(parse_qsl(split.query, keep_blank_values=True))
    return f"{method.upper()} {split.path}?{urlencode(pairs)}"


def reject_secret_query(key: str) -> None:
    """Refuse to persist exchanges whose query looks like it carries credentials."""
    query = urlsplit(key.split(" ", 1)[1]).query
    for name, _ in parse_qsl(query, keep_blank_values=True):
        if name.lower() in SECRET_QUERY_NAMES:
            raise SessionSanitizationError(
                f"refusing to record exchange with credential-like query parameter {name!r}"
            )


def encode_body(body: bytes) -> tuple[str, str]:
    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(body).decode("ascii"), "base64"


def decode_body(record: Mapping[str, Any]) -> bytes:
    encoding = record.get("body_encoding", "utf-8")
    if encoding == "base64":
        return base64.b64decode(record["body"])
    if encoding == "utf-8":
        return record["body"].encode("utf-8")
    raise SessionFormatError(f"unknown body encoding {encoding!r}")


class SessionRecorder:
    """Append recorded exchanges to a JSONL session file; thread-safe."""

    def __init__(self, path: Path, *, provider: str, upstream: str) -> None:
        self.path = path
        self.provider = provider
        self.upstream = upstream
        self._lock = threading.Lock()
        self._key_counts: dict[str, int] = defaultdict(int)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8")
        self._write(
            {
                "type": "header",
                "version": SESSION_VERSION,
                "provider": provider,
                "upstream": upstream,
                "recorded_at": utc_now(),
            }
        )

    def _write(self, record: Mapping[str, Any]) -> None:
        self._file.write(json.dumps(dict(record), sort_keys=True) + "\n")
        self._file.flush()

    def append(
        self,
        *,
        key: str,
        status: int,
        content_type: str | None,
        retry_after: str | None,
        body: bytes,
    ) -> None:
        reject_secret_query(key)
        text, encoding = encode_body(body)
        with self._lock:
            self._write(
                {
                    "type": "exchange",
                    "key": key,
                    "status": status,
                    "content_type": content_type,
                    "retry_after": retry_after,
                    "body": text,
                    "body_encoding": encoding,
                }
            )
            self._key_counts[key] += 1

    @property
    def exchanges(self) -> int:
        with self._lock:
            return sum(self._key_counts.values())

    def key_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._key_counts)

    def close(self) -> None:
        self._file.close()


def load_session(path: Path) -> tuple[dict[str, Any], dict[str, deque[dict[str, Any]]]]:
    """Load a JSONL session file into a header and per-key exchange queues."""
    header: dict[str, Any] | None = None
    exchanges: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    with path.open(encoding="utf-8") as session:
        for number, line in enumerate(session, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise SessionFormatError(f"{path}:{number}: invalid JSON: {error}") from error
            kind = record.get("type")
            if kind == "header":
                if header is not None:
                    raise SessionFormatError(f"{path}:{number}: duplicate session header")
                if record.get("version") != SESSION_VERSION:
                    raise SessionFormatError(
                        f"{path}:{number}: unsupported session version {record.get('version')!r}"
                    )
                if record.get("provider") not in PROVIDER_EMPTY_BODIES:
                    raise SessionFormatError(
                        f"{path}:{number}: unknown provider {record.get('provider')!r}"
                    )
                header = record
            elif kind == "exchange":
                if header is None:
                    raise SessionFormatError(f"{path}:{number}: exchange before session header")
                for field in ("key", "status", "body"):
                    if field not in record:
                        raise SessionFormatError(
                            f"{path}:{number}: exchange is missing field {field!r}"
                        )
                exchanges[record["key"]].append(record)
            else:
                raise SessionFormatError(f"{path}:{number}: unknown record type {kind!r}")
    if header is None:
        raise SessionFormatError(f"{path}: missing session header")
    return header, dict(exchanges)


class Served(NamedTuple):
    status: int
    content_type: str
    retry_after: str | None
    body: bytes
    disposition: str
    remaining: int | None


class ReplayStore:
    """Serve recorded exchanges per canonical key, in recorded order."""

    def __init__(
        self,
        exchanges: Mapping[str, deque[dict[str, Any]]],
        *,
        provider: str,
        on_exhausted: str = "empty",
        on_unknown: str = "error",
    ) -> None:
        self._exchanges = {key: deque(records) for key, records in exchanges.items()}
        self._provider = provider
        self._on_exhausted = on_exhausted
        self._on_unknown = on_unknown
        self._lock = threading.Lock()
        self.hits = 0
        self.exhausted = 0
        self.unknown = 0

    def _empty(self, disposition: str, remaining: int | None = None) -> Served:
        body = PROVIDER_EMPTY_BODIES[self._provider].encode("utf-8")
        return Served(200, "application/json", None, body, disposition, remaining)

    def _reject(self, detail: str) -> Served:
        body = json.dumps({"error": detail}).encode("utf-8")
        return Served(404, "application/json", None, body, "error", 0)

    def take(self, key: str) -> Served:
        with self._lock:
            queue = self._exchanges.get(key)
            if queue is None:
                self.unknown += 1
                if self._on_unknown == "empty":
                    return self._empty("unknown-empty")
                return self._reject("request was not recorded in this session")
            if queue:
                record = queue.popleft()
                self.hits += 1
                return Served(
                    record["status"],
                    record.get("content_type") or "application/json",
                    record.get("retry_after"),
                    decode_body(record),
                    "hit",
                    len(queue),
                )
            self.exhausted += 1
            if self._on_exhausted == "empty":
                return self._empty("exhausted-empty")
            return self._reject("recorded exchanges for this request are exhausted")

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "keys_recorded": len(self._exchanges),
                "served": self.hits,
                "exhausted": self.exhausted,
                "unknown": self.unknown,
                "remaining_exchanges": sum(len(queue) for queue in self._exchanges.values()),
            }


class ProviderReplayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        mode: str,
        recorder: SessionRecorder | None = None,
        store: ReplayStore | None = None,
        upstream: str | None = None,
        opener: Any = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.mode = mode
        self.recorder = recorder
        self.store = store
        self.upstream = upstream
        self.opener = opener


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "provider-replay/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        LOG.debug("%s", format % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        server = cast(ProviderReplayServer, self.server)
        if server.mode == "record":
            self._record(server)
        else:
            self._replay(server)

    def _method_not_allowed(self) -> None:
        self._send(501, "application/json", None, b'{"error": "only GET is supported"}')

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_not_allowed

    def _record(self, server: ProviderReplayServer) -> None:
        assert server.recorder is not None and server.upstream is not None
        key = canonical_key("GET", self.path)
        url = server.upstream.rstrip("/") + self.path
        headers = {}
        for name in ("User-Agent", "Accept"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        request = UrlRequest(url, headers=headers, method="GET")
        try:
            with server.opener.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                status = response.status
                body = response.read()
                content_type = response.headers.get("Content-Type")
                retry_after = response.headers.get("Retry-After")
        except HTTPError as error:
            # Provider 4xx/5xx are recorded reality, not capture failures.
            status = error.code
            body = error.read()
            content_type = error.headers.get("Content-Type")
            retry_after = error.headers.get("Retry-After")
        except (URLError, OSError) as error:
            LOG.warning("upstream failure for %s: %s", url, error)
            status, body, content_type, retry_after = 502, b"", "application/json", None
        try:
            server.recorder.append(
                key=key,
                status=status,
                content_type=content_type,
                retry_after=retry_after,
                body=body,
            )
        except SessionSanitizationError as error:
            LOG.error("%s", error)
            self._send(500, "application/json", None, b'{"error": "session sanitization"}')
            return
        LOG.info("recorded %s -> %s (%d bytes)", key, status, len(body))
        self._send(status, content_type or "application/json", retry_after, body)

    def _replay(self, server: ProviderReplayServer) -> None:
        assert server.store is not None
        key = canonical_key("GET", self.path)
        served = server.store.take(key)
        if served.disposition == "hit":
            LOG.info("replay %s -> %s (%s remaining)", key, served.status, served.remaining)
        elif served.disposition == "exhausted-empty":
            LOG.info("replay %s -> empty (session exhausted)", key)
        else:
            LOG.warning("replay %s -> %s (%s)", key, served.status, served.disposition)
        self._send(served.status, served.content_type, served.retry_after, served.body)

    def _send(
        self,
        status: int,
        content_type: str,
        retry_after: str | None,
        body: bytes,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    record = subparsers.add_parser(
        "record",
        help="forward provider requests to the live upstream and write a session",
    )
    record.add_argument("--provider", choices=sorted(PROVIDER_UPSTREAMS), required=True)
    record.add_argument("--session", type=Path, required=True)
    record.add_argument("--host", default="127.0.0.1")
    record.add_argument("--port", type=int, default=DEFAULT_PORT)
    record.add_argument("--upstream", help="override the provider upstream base URL")
    record.add_argument("--proxy", help="HTTP(S) proxy for upstream requests")
    record.add_argument("--report", type=Path)
    record.add_argument("--quiet", action="store_true")

    replay = subparsers.add_parser(
        "replay",
        help="serve a recorded session without touching the provider",
    )
    replay.add_argument("--session", type=Path, required=True)
    replay.add_argument("--host", default="127.0.0.1")
    replay.add_argument("--port", type=int, default=DEFAULT_PORT)
    replay.add_argument(
        "--on-exhausted",
        choices=("empty", "error"),
        default="empty",
        help="response once a request's recorded exchanges run out (default: empty)",
    )
    replay.add_argument(
        "--on-unknown",
        choices=("empty", "error"),
        default="error",
        help="response for a request that was never recorded (default: error)",
    )
    replay.add_argument("--report", type=Path)
    replay.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def _install_shutdown(server: ProviderReplayServer) -> None:
    def handler(signum: int, _frame: Any) -> None:
        LOG.info("shutting down on signal %s", signum)
        # shutdown() must run off the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.mode == "record":
        upstream = args.upstream or PROVIDER_UPSTREAMS[args.provider]
        if args.session.exists():
            LOG.warning("overwriting existing session %s", args.session)
        opener = (
            build_opener(ProxyHandler({"http": args.proxy, "https": args.proxy}))
            if args.proxy
            else build_opener()
        )
        recorder = SessionRecorder(args.session, provider=args.provider, upstream=upstream)
        server = ProviderReplayServer(
            (args.host, args.port),
            mode="record",
            recorder=recorder,
            upstream=upstream,
            opener=opener,
        )
    else:
        header, exchanges = load_session(args.session)
        store = ReplayStore(
            exchanges,
            provider=header["provider"],
            on_exhausted=args.on_exhausted,
            on_unknown=args.on_unknown,
        )
        server = ProviderReplayServer((args.host, args.port), mode="replay", store=store)

    _install_shutdown(server)
    host, port = server.server_address[:2]
    LOG.info("%s: listening on http://%s:%s (session=%s)", args.mode, host, port, args.session)
    try:
        server.serve_forever()
    finally:
        server.server_close()

    if args.mode == "record":
        assert server.recorder is not None
        server.recorder.close()
        report = {
            "mode": "record",
            "session": str(args.session),
            "provider": server.recorder.provider,
            "upstream": server.recorder.upstream,
            "exchanges_recorded": server.recorder.exchanges,
            "keys": server.recorder.key_counts(),
        }
    else:
        assert server.store is not None
        report = {"mode": "replay", "session": str(args.session), **server.store.report()}

    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
