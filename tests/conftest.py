"""A scripted local HTTP server standing in for vendor APIs and GitHub: tests exercise real urllib traffic."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    """Serialised as JSON unless it is ``bytes``."""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    """Seconds to wait before answering (for timeout tests)."""


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


class FakeServer:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[Seen] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.do_POST()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                server.requests.append(
                    Seen(
                        self.command,
                        self.path,
                        {k.lower(): v for k, v in self.headers.items()},
                        json.loads(raw) if raw else None,
                    )
                )
                reply = (
                    server.replies.pop(0) if server.replies else Reply(500, {"message": "no reply queued"})
                )
                if reply.delay:
                    time.sleep(reply.delay)
                payload = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
                try:
                    self.send_response(reply.status)
                    for name, value in reply.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client timed out and hung up

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def reply(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_server() -> Iterator[FakeServer]:
    server = FakeServer()
    try:
        yield server
    finally:
        server.close()
