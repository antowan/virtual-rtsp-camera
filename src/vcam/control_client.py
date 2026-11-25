"""Small synchronous HTTP client for VCAM control; no server dependencies."""

from __future__ import annotations

import ipaddress
import json
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .errors import VcamError

MAX_RESPONSE = 16 * 1024 * 1024
MAX_EVENT = 64 * 1024


class ControlClientError(VcamError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class ControlEvent:
    id: str
    event: str
    data: Any


class _NoRedirect(HTTPRedirectHandler):
    # A control endpoint must never get to choose where we send its token.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _base_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if (
        not host
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port == 0
        or parsed.path.rstrip("/") not in {"", "/api/v1"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "expected an http(s) control endpoint without credentials, query or fragment"
        )
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local and parsed.scheme != "https":
        raise ValueError("remote control endpoints require HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}/api/v1"


def _header_value(value: str) -> str:
    if not value or not value.isascii() or not value.isprintable() or value.strip() != value:
        raise ValueError("invalid control request header value")
    return value


class _Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


def _json(stream: _Readable, limit: int = MAX_RESPONSE) -> Any:
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ControlClientError("oversized control response", code="invalid_response")
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ControlClientError(
            "invalid JSON from control endpoint", code="invalid_response"
        ) from exc


def _error_from(payload: Any, default_code: str, status: int | None = None) -> ControlClientError:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = {}
    message = error.get("message")
    code = error.get("code")
    details = error.get("details")
    return ControlClientError(
        message if isinstance(message, str) else "control request failed",
        code=code if isinstance(code, str) else default_code,
        status=status,
        details=details if isinstance(details, dict) else None,
    )


class ControlClient:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 5,
    ) -> None:
        self.base_url = _base_url(endpoint)
        if timeout <= 0:
            raise ValueError("control timeout must be positive")
        if ssl_context is not None and (
            not ssl_context.check_hostname or ssl_context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ValueError("HTTPS must verify the certificate and hostname")
        self.timeout = timeout
        self._authorization = "Bearer " + _header_value(token)
        self._opener = build_opener(
            ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl_context)
        )

    def _url(self, path: str) -> str:
        relative = path.removeprefix("/api/v1/").removeprefix("/")
        parsed = urlsplit(relative)
        if (
            parsed.scheme
            or parsed.netloc
            or path.startswith("//")
            or parsed.fragment
            or any(p in {".", ".."} for p in unquote(parsed.path).split("/"))
        ):
            raise ValueError("expected a path relative to the control API")
        return f"{self.base_url}/{relative}"

    def _open(self, request: Request, timeout: float):
        request.add_unredirected_header("Authorization", self._authorization)
        try:
            return self._opener.open(request, timeout=timeout)
        except HTTPError as exc:
            with exc:
                try:
                    payload = _json(exc, MAX_EVENT)
                except (ControlClientError, HTTPException, OSError):
                    payload = None
            raise _error_from(payload, "http_error", exc.code) from exc
        except (URLError, HTTPException, OSError) as exc:
            raise ControlClientError("cannot reach control endpoint", code="unavailable") from exc

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        revision: str | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if revision is not None:
            headers["If-Match"] = _header_value(revision)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _header_value(idempotency_key)
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, allow_nan=False).encode()
        request = Request(self._url(path), body, headers, method=method)
        try:
            with self._open(request, self.timeout if timeout is None else timeout) as response:
                return _json(response)
        except (HTTPException, OSError) as exc:
            raise ControlClientError("interrupted control response", code="unavailable") from exc

    def wait(self, operation_id: str, *, timeout: float = 60) -> dict[str, Any]:
        """Wait for a definitive result; expiry does not cancel the operation."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            op = self.request(
                "GET",
                f"operations/{quote(operation_id, safe='')}",
                timeout=min(remaining, self.timeout),
            )
            if not isinstance(op, dict):
                raise ControlClientError("invalid operation response", code="invalid_response")
            state = op.get("status")
            if state == "succeeded":
                return op
            if state in ("failed", "cancelled"):
                failure = _error_from(op, "operation_" + state)
                failure.details.update(
                    {k: op[k] for k in ("config_persisted", "applied", "id") if k in op}
                )
                raise failure
            if state not in ("queued", "running"):
                raise ControlClientError("unrecognized operation state", code="invalid_response")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        raise ControlClientError(
            "timed out waiting for operation; poll its ID to check the outcome",
            code="wait_timeout",
            details={"operation_id": operation_id},
        )

    def events(self, last_event_id: str | None = None) -> Iterator[ControlEvent]:
        """Read one subscription. Reconnect explicitly using the last dispatched ID."""
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = _header_value(last_event_id)
        request = Request(self._url("events"), headers=headers)
        try:
            with self._open(request, max(45, self.timeout)) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ControlClientError("expected an event stream", code="invalid_response")
                yield from _read_events(response, last_event_id or "")
        except (HTTPException, OSError) as exc:
            raise ControlClientError("interrupted event stream", code="unavailable") from exc


def _read_events(stream: BinaryIO, cursor: str) -> Iterator[ControlEvent]:
    event = "message"
    data: list[str] = []
    size = 0
    while line := stream.readline(MAX_EVENT + 1):
        size += len(line)
        if size > MAX_EVENT:
            raise ControlClientError("oversized control event", code="invalid_response")
        try:
            text = line.decode("utf-8").rstrip("\r\n")
        except UnicodeError as exc:
            raise ControlClientError("invalid event encoding", code="invalid_response") from exc
        if not text:
            if data:
                try:
                    value = json.loads("\n".join(data))
                except ValueError as exc:
                    raise ControlClientError("invalid event JSON", code="invalid_response") from exc
                yield ControlEvent(cursor, event, value)
            event, data, size = "message", [], 0
            continue
        field, _, content = text.partition(":")
        content = content.removeprefix(" ")
        if field == "event":
            event = content or "message"
        elif field == "id" and "\x00" not in content:
            cursor = content
        elif field == "data":
            data.append(content)
