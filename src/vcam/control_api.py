"""FastAPI application and Uvicorn thread for the optional control API.

Imported only when control mode is enabled: ``fastapi`` and ``uvicorn`` come
from the ``control`` extra. Handlers stay thin and call ``Controller``, which
submits typed commands to the supervisor's owner thread.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import APIRouter, Body, FastAPI, Header, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Message, Receive, Scope, Send

from . import __version__, diagnostics
from .control import ControlError, Controller
from .control_models import (
    Action,
    ApplyRequest,
    CameraDetail,
    CameraList,
    CameraPatch,
    ConfigView,
    Confirmation,
    EngineAction,
    EngineStatus,
    ErrorEnvelope,
    OperationResult,
    PlanRequest,
    PlanResult,
    ProbeCamera,
    ProbeSource,
    ReadyView,
    ReloadRequest,
    StreamLink,
    StreamLinks,
)
from .errors import ProbeError
from .models import LOOPBACK_HOSTS, CameraSpec, CameraStack

logger = logging.getLogger("vcam")

API_PREFIX = "/api/v1"
MAX_BODY = 1024 * 1024
MAX_SSE_CLIENTS = 16
KEEPALIVE_SECONDS = 15.0
NAME = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PORT = Path(ge=1, le=65535)
REQUIRED_BODY = Body(...)
OPTIONAL_BODY = Body(default=None)


def _envelope(
    status: int, code: str, message: str, request_id: str, details: Any = None
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": details or {},
            }
        },
        status_code=status,
        headers={"X-Request-ID": request_id},
    )


class _Guard:
    """Pure-ASGI gate: auth, Host/Origin checks and the body size limit.

    Running before routing means unauthenticated callers learn nothing about
    which routes exist, and nothing is buffered beyond ``MAX_BODY``.
    """

    def __init__(self, app: FastAPI, controller: Controller, check_host: bool) -> None:
        self.app = app
        self.controller = controller
        self.check_host = check_host
        self.hosts = LOOPBACK_HOSTS.union(
            {controller.spec.host.lower()}, controller.spec.allowed_hosts
        ) - {"0.0.0.0", "::"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        failure = self._reject(headers, scope["method"])
        if failure is not None:
            status, code, reason = failure
            await _envelope(status, code, reason, request_id)(scope, receive, send)
            return
        length = headers.get("content-length")
        if length is not None and (
            not length.isascii()
            or not length.isdigit()
            or len(length) > 10
            or int(length) > MAX_BODY
        ):
            await _envelope(413, "body_too_large", "request body too large", request_id)(
                scope, receive, send
            )
            return
        incoming = receive
        if scope["method"] in {"POST", "PATCH", "DELETE"}:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_BODY:
                    await _envelope(413, "body_too_large", "request body too large", request_id)(
                        scope, receive, send
                    )
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            if body and headers.get("content-type", "").split(";")[0].strip().lower() not in {
                "application/json",
                "application/merge-patch+json",
            }:
                await _envelope(415, "unsupported_media_type", "send a JSON body", request_id)(
                    scope, receive, send
                )
                return

            async def buffered() -> Message:
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            incoming = buffered

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"cache-control", b"no-store"),
                ]
                if not any(k == b"x-request-id" for k, _ in message["headers"]):
                    message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, incoming, tagged)

    def _reject(self, headers: dict[str, str], method: str) -> tuple[int, str, str] | None:
        if self.check_host:
            try:
                host = urlsplit("//" + headers.get("host", ""))
                host_ok = (
                    host.hostname in self.hosts
                    and host.username is None
                    and host.port != 0
                    and not (host.query or host.path or host.fragment)
                )
            except ValueError:
                host_ok = False
            if not host_ok:
                return 403, "host_forbidden", "unexpected Host header"
        if "origin" in headers:
            if headers["origin"] not in self.controller.spec.allowed_origins:
                return 403, "origin_forbidden", "origin is not allowed"
            if method == "OPTIONS" and "access-control-request-method" in headers:
                return None  # Exact-origin preflight is handled by CORSMiddleware, not a route.
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or not self.controller.check_token(token):
            return 401, "unauthorized", "missing or invalid bearer token"
        return None


def create_app(controller: Controller, *, check_host: bool = True) -> _Guard:
    """Build the authenticated control API around ``controller``."""
    app = FastAPI(
        title="vcam control API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.closing = threading.Event()
    app.state.sse_clients = 0
    if controller.spec.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=controller.spec.allowed_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "If-Match",
                "Idempotency-Key",
                "Last-Event-ID",
            ],
            expose_headers=["ETag", "Location", "X-Request-ID"],
        )

    def request_id(request: Request) -> str:
        return str(request.scope.get("state", {}).get("request_id", ""))

    @app.exception_handler(ControlError)
    async def _control_error(request: Request, exc: ControlError) -> JSONResponse:
        # Parser and model exception messages can quote entire secret-bearing lines.
        message = {
            "invalid_config": "invalid configuration",
            "invalid_camera": "invalid camera definition",
        }.get(exc.code, exc.message)
        return _envelope(exc.status, exc.code, message, request_id(request), exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "code": e["type"]} for e in exc.errors()]
        malformed = any(e["code"] == "json_invalid" for e in errors)
        return _envelope(
            400 if malformed else 422,
            "invalid_request",
            "invalid request",
            request_id(request),
            {"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _envelope(exc.status_code, code, str(exc.detail), request_id(request))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("control API request %s failed (%s)", request_id(request), type(exc).__name__)
        return _envelope(500, "internal_error", "internal error", request_id(request))

    def reject_redacted(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                reject_redacted(item)
        elif isinstance(value, str) and "[REDACTED]" in value:
            raise ControlError(422, "redacted_input", "supply the actual value or omit this field")

    def accepted(operation: dict[str, Any]) -> JSONResponse:
        return JSONResponse(
            operation,
            status_code=202,
            headers={"Location": f"{API_PREFIX}/operations/{operation['id']}"},
        )

    def tagged(payload: dict[str, Any], revision: str | None) -> JSONResponse:
        headers = {"ETag": f'"{revision}"'} if revision else {}
        return JSONResponse(payload, headers=headers)

    api = APIRouter(responses={"default": {"model": ErrorEnvelope}})

    @api.get("/openapi.json", include_in_schema=False)
    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "ControlToken": {"type": "http", "scheme": "bearer"}
            }
            schema["security"] = [{"ControlToken": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    @api.get("/status", response_model=EngineStatus)
    def status() -> dict[str, Any]:
        return {
            **controller.status(),
            "api_version": 1,
            "capabilities": ["camera-actions", "camera-crud", "config-plan", "diagnostics", "sse"],
        }

    @api.get("/health")
    def health() -> dict[str, Any]:
        _, observed = controller.health()
        return {**observed, "servers": controller.snapshot().get("servers", [])}

    @api.get("/ready", response_model=ReadyView, responses={503: {"model": ReadyView}})
    def ready() -> JSONResponse:
        ok, payload = controller.ready()
        ok = ok and not app.state.closing.is_set()
        return JSONResponse({**payload, "ready": ok}, status_code=200 if ok else 503)

    @api.get("/schema/config")
    def config_schema() -> dict[str, Any]:
        return CameraStack.model_json_schema()

    @api.get("/cameras", response_model=CameraList)
    def list_cameras() -> JSONResponse:
        cameras, revision = controller.cameras()
        return tagged(
            {
                "revision": revision,
                "cameras": sorted(cameras, key=lambda camera: (camera["port"], camera["name"])),
            },
            revision,
        )

    @api.post("/cameras", status_code=202, response_model=OperationResult)
    def create_camera(
        body: CameraSpec,
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        values = body.model_dump(mode="json", exclude_unset=True)
        reject_redacted(values)
        return accepted(
            controller.create_camera(
                values,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )
        )

    @api.get("/cameras/{port}/{name}", response_model=CameraDetail)
    def get_camera(port: int = PORT, name: str = NAME) -> JSONResponse:
        camera, revision = controller.camera(port, name)
        return tagged({"revision": revision, "camera": camera}, revision)

    @api.patch("/cameras/{port}/{name}", status_code=202, response_model=OperationResult)
    def update_camera(
        port: int = PORT,
        name: str = NAME,
        patch: CameraPatch = REQUIRED_BODY,
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        values = jsonable_encoder(patch, exclude_unset=True)
        reject_redacted(values)
        return accepted(
            controller.update_camera(
                port,
                name,
                values,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )
        )

    @api.delete("/cameras/{port}/{name}", status_code=202, response_model=OperationResult)
    def delete_camera(
        port: int = PORT,
        name: str = NAME,
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        return accepted(
            controller.delete_camera(port, name, if_match=if_match, idempotency_key=idempotency_key)
        )

    @api.post("/cameras/{port}/{name}/actions", status_code=202, response_model=OperationResult)
    def camera_actions(
        body: Action,
        port: int = PORT,
        name: str = NAME,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        return accepted(
            controller.camera_action(port, name, body.action, idempotency_key=idempotency_key)
        )

    @api.post("/cameras/{port}/{name}/{action}", status_code=202, response_model=OperationResult)
    def camera_action(
        port: int = PORT,
        name: str = NAME,
        action: str = Path(pattern="^(start|stop|restart|enable|disable)$"),
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        if action in {"enable", "disable"}:
            return accepted(
                controller.update_camera(
                    port,
                    name,
                    {"enabled": action == "enable"},
                    if_match=if_match,
                    idempotency_key=idempotency_key,
                )
            )
        return accepted(
            controller.camera_action(port, name, action, idempotency_key=idempotency_key)
        )

    @api.post("/engine/actions", status_code=202, response_model=OperationResult)
    def engine_actions(
        body: EngineAction, idempotency_key: str | None = Header(default=None)
    ) -> JSONResponse:
        return accepted(
            controller.engine_action(
                body.action, allow_disruption=body.allow_disruption, idempotency_key=idempotency_key
            )
        )

    @api.post("/engine/{action}", status_code=202, response_model=OperationResult)
    def engine_action(
        action: str = Path(pattern="^(start|stop|restart)$"),
        body: Confirmation | None = None,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        allow = body.allow_disruption if body else False
        return accepted(
            controller.engine_action(
                action, allow_disruption=allow, idempotency_key=idempotency_key
            )
        )

    @api.get("/replays")
    def replays() -> dict[str, Any]:
        return {"replays": controller.replays()}

    @api.get("/replays/{port}/{name}")
    def replay_detail(port: int = PORT, name: str = NAME) -> dict[str, Any]:
        for replay in controller.replays():
            if (replay["port"], replay["name"]) == (port, name):
                return {"replay": replay}
        raise ControlError(404, "replay_not_found", "no replay at this port and name")

    @api.get("/sync-groups")
    def sync_groups() -> dict[str, Any]:
        return {"sync_groups": controller.sync_groups()}

    @api.get("/sync-groups/{name}")
    def sync_detail(name: str = NAME) -> dict[str, Any]:
        for group in controller.sync_groups():
            if group["name"] == name:
                return {"sync_group": group}
        raise ControlError(404, "sync_group_not_found", "no such sync group")

    @api.get("/urls")
    def urls(
        host: str | None = Query(default=None, max_length=253), include_disabled: bool = False
    ) -> StreamLinks:
        stack = controller.target_stack
        display = host if host is not None else stack.display_host()
        try:
            address = ipaddress.ip_address(display.strip("[]") if ":" in display else display)
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", display):
                raise ControlError(
                    422, "invalid_display_host", "expected a hostname or IP"
                ) from None
        else:
            display = f"[{address}]" if address.version == 6 else str(address)
        streams = [
            StreamLink(
                name=c.name,
                port=stack.effective_port(c),
                kind="camera",
                url=stack.read_url(c, display, with_credentials=False),
            )
            for c in (stack.cameras if include_disabled else stack.enabled_cameras)
        ] + [
            StreamLink(
                name=r.name,
                port=r.port,
                kind="replay",
                url=stack.replay_url(r, display, with_credentials=False),
            )
            for r in (stack.replays if include_disabled else stack.enabled_replays)
        ]
        return StreamLinks(urls=[s.url for s in streams], streams=streams)

    @api.get("/config", response_model=ConfigView)
    def config() -> JSONResponse:
        payload = controller.config()
        return tagged(payload, payload["revision"])

    @api.post("/config/plan", response_model=PlanResult)
    def plan(
        body: PlanRequest | None = None,
        if_match: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if body:
            reject_redacted(body.text)
        return controller.plan(body.text if body else None, if_match=if_match)

    @api.post("/config/apply", status_code=202, response_model=OperationResult)
    def apply(
        body: ApplyRequest,
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        return accepted(
            controller.apply(
                body.plan_id,
                if_match=if_match,
                allow_disruption=body.allow_disruption,
                idempotency_key=idempotency_key,
            )
        )

    @api.post("/config/reload", response_model=PlanResult)
    def reload(body: ReloadRequest | None = None) -> dict[str, Any]:
        if body and not body.preview:
            raise ControlError(409, "preview_required", "preview the file, then apply the plan")
        return controller.reload()

    @api.get("/operations/{operation_id}", response_model=OperationResult)
    def operation(operation_id: str = Path(max_length=64)) -> dict[str, Any]:
        return controller.operation(operation_id)

    @api.post("/diagnostics/doctor")
    def doctor() -> dict[str, Any]:
        runtime = controller.supervisor
        return diagnostics.doctor(runtime.ffmpeg, runtime.ffprobe, runtime.mediamtx_binary)

    @api.post("/diagnostics/probe")
    def probe(body: ProbeCamera | ProbeSource) -> dict[str, Any]:
        if not controller.media_slots.acquire(blocking=False):
            raise ControlError(429, "diagnostics_busy", "all probe workers are busy")
        try:
            if isinstance(body, ProbeCamera):
                stack = controller.target_stack
                for camera in stack.cameras:
                    if (stack.effective_port(camera), camera.name) == (
                        body.camera.port,
                        body.camera.name,
                    ):
                        break
                else:
                    raise ControlError(404, "camera_not_found", "no camera at this port and name")
            else:
                camera = CameraSpec(name="probe", source=body.source)
            base = controller.config_path.parent if controller.config_path else FilePath.cwd()
            return diagnostics.media_probe(camera, base, controller.supervisor.ffprobe)
        except (ProbeError, ValidationError):
            raise ControlError(422, "probe_failed", "cannot probe this media source") from None
        finally:
            controller.media_slots.release()

    @api.get("/logs")
    def logs(
        limit: int = Query(default=200, ge=1, le=2000),
        since: int = Query(default=0, ge=0),
        level: str | None = Query(default=None, pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
        resource: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, Any]:
        return controller.logs.page(limit, since, level, resource)

    @api.get(
        "/events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    async def events(
        request: Request, last_event_id: str | None = Header(default=None)
    ) -> StreamingResponse:
        seq = controller.events.parse_cursor(last_event_id)
        if app.state.sse_clients >= MAX_SSE_CLIENTS:
            raise ControlError(429, "too_many_subscribers", "too many event subscribers")
        app.state.sse_clients += 1

        async def stream():
            nonlocal seq
            try:
                last_sent = time.monotonic()
                while not app.state.closing.is_set():
                    pending = controller.events.since(seq)
                    if pending and pending[0]["seq"] > seq + 1:
                        # Fell behind the ring buffer: tell the client to resync.
                        yield "event: resync\ndata: {}\n\n"
                        return
                    for event in pending:
                        seq = event["seq"]
                        payload = {"time": event["time"], **event["data"]}
                        yield (
                            f"id: {event['id']}\nevent: {event['event']}\n"
                            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                        )
                        last_sent = time.monotonic()
                    if time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last_sent = time.monotonic()
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(0.25)
            finally:
                app.state.sse_clients -= 1

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    app.include_router(api, prefix=API_PREFIX)
    return _Guard(app, controller, check_host=check_host)


class ControlServer:
    """Serves the control API from a daemon thread named ``vcam-control-api``."""

    def __init__(self, controller: Controller) -> None:
        spec = controller.spec
        self.controller = controller
        self.app = create_app(controller)
        family = socket.AF_INET6 if ":" in spec.host else socket.AF_INET
        self.socket = socket.socket(family, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.bind((spec.host, spec.port))
            self.socket.listen(64)
        except OSError:
            self.socket.close()
            raise
        self.port = self.socket.getsockname()[1]
        local = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(spec.host, spec.host)
        host = f"[{local}]" if ":" in local else local
        self.endpoint = f"{spec.scheme}://{host}:{self.port}"
        ssl_kwargs: dict[str, Any] = {}
        if spec.tls_cert is not None and spec.tls_key is not None:
            ssl_kwargs = {
                "ssl_certfile": str(spec.tls_cert),
                "ssl_keyfile": str(spec.tls_key),
                "ssl_version": ssl.PROTOCOL_TLS_SERVER,
            }
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                http="h11",
                loop="asyncio",
                ws="none",
                interface="asgi3",
                workers=1,
                proxy_headers=False,
                log_config=None,
                access_log=False,
                lifespan="off",
                timeout_graceful_shutdown=2,
                server_header=False,
                **ssl_kwargs,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.socket]},
            name="vcam-control-api",
            daemon=True,
        )

    def start(self, timeout: float = 10.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("control API failed to start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.app.app.state.closing.set()
        self.server.should_exit = True
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("control API did not stop")
