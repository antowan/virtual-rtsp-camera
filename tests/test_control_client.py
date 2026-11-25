from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from vcam.control_client import ControlClient, ControlClientError, ControlEvent


@pytest.fixture
def endpoint() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def _reply(self, status: int, content: bytes, mime: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "if-match": self.headers.get("If-Match"),
                    "key": self.headers.get("Idempotency-Key"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self._reply(202, b'{"id":"add","status":"queued"}')

        def do_GET(self) -> None:
            requests.append({"path": self.path, "cursor": self.headers.get("Last-Event-ID")})
            if self.path == "/api/v1/redirect":
                self.send_response(307)
                self.send_header("Location", "/api/v1/token-sink")
                self.end_headers()
            elif self.path == "/api/v1/events":
                self._reply(
                    200,
                    b': keepalive\n\nid: engine:7\nevent: config\ndata: {"ok":\ndata: true}\n\n',
                    "text/event-stream",
                )
            elif self.path == "/api/v1/operations/add":
                count = sum(req["path"] == self.path for req in requests)
                status = "running" if count == 1 else "succeeded"
                self._reply(200, json.dumps({"id": "add", "status": status}).encode())
            elif self.path == "/api/v1/operations/partial":
                self._reply(
                    200,
                    b'{"id":"partial","status":"failed","config_persisted":true,"applied":false,'
                    b'"error":{"code":"bind_failed","message":"port unavailable","details":{}}}',
                )
            else:
                self._reply(412, b'{"error":{"code":"revision_conflict","message":"stale"}}')

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, name="vcam-test-transport")
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_json_requests_and_operation_polling(endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    url, requests = endpoint
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    client = ControlClient(url + "/api/v1/", token="example")
    op = client.request(
        "POST", "/cameras", {"name": "lobby"}, revision='"abc"', idempotency_key="x"
    )
    assert requests[0] == {
        "path": "/api/v1/cameras",
        "authorization": "Bearer example",
        "if-match": '"abc"',
        "key": "x",
        "body": {"name": "lobby"},
    }
    assert client.wait(op["id"])["status"] == "succeeded"


def test_api_errors_are_distinct_from_accepted_operation_failure(endpoint) -> None:
    url, _ = endpoint
    client = ControlClient(url, token="example")
    with pytest.raises(ControlClientError, match="stale") as stale:
        client.request("GET", "/api/v1/config")
    assert (stale.value.status, stale.value.code) == (412, "revision_conflict")
    with pytest.raises(ControlClientError, match="port unavailable") as partial:
        client.wait("partial")
    assert partial.value.code == "bind_failed"
    assert partial.value.details == {"id": "partial", "config_persisted": True, "applied": False}


def test_sse_decodes_events_and_forwards_opaque_cursor(endpoint) -> None:
    url, requests = endpoint
    client = ControlClient(url, token="example")
    assert list(client.events("engine:6")) == [ControlEvent("engine:7", "config", {"ok": True})]
    assert requests == [{"path": "/api/v1/events", "cursor": "engine:6"}]


def test_client_does_not_follow_redirects_or_accept_other_origins(endpoint) -> None:
    url, requests = endpoint
    client = ControlClient(url, token="example")
    with pytest.raises(ControlClientError) as redirect:
        client.request("GET", "/redirect")
    assert redirect.value.status == 307
    for path in ("//elsewhere/path", "https://elsewhere", "/%2e%2e/other"):
        with pytest.raises(ValueError):
            client.request("GET", path)
    assert [req["path"] for req in requests] == ["/api/v1/redirect"]


def test_endpoint_rejects_cleartext_remote_credentials_and_insecure_tls() -> None:
    for endpoint in ("http://example.test", "https://name:pass@example.test", "file:///example"):
        with pytest.raises(ValueError):
            ControlClient(endpoint, token="example")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    with pytest.raises(ValueError, match="verify"):
        ControlClient("https://example.test", token="example", ssl_context=context)
