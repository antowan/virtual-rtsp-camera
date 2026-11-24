"""Control-plane state shared by the HTTP API: no web framework imported here.

The supervisor's monitor thread owns the runtime. This module only reads the
snapshots it publishes (``Controller.tick`` runs on that thread) and submits
typed commands to it. YAML stays the source of truth: every mutation is a
comment-preserving edit committed against the revision the caller last saw.
"""

from __future__ import annotations

import collections
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import platform
import queue
import secrets
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from ruamel.yaml.comments import CommentedMap

from . import __version__
from .config import format_validation_error, parse_stack_text
from .config_store import (
    ConfigDocument,
    ConfigMigrationRequired,
    ConfigRevisionConflict,
    ConfigStore,
    ConfigWriteError,
    camera_fields,
    camera_nodes,
)
from .errors import ConfigError, SupervisorError, VcamError
from .mediamtx import MediaMTXAPIError, MediaMTXClient
from .models import CameraSpec, CameraStack, ControlSpec
from .reconcile import (
    CameraKey,
    ChangePlan,
    MediaAdmission,
    camera_key,
    check_listeners,
    file_stamp,
    plan_changes,
    preflight,
)
from .sources import display_source, redact_text
from .supervisor import (
    ApplyCommand,
    ApplyError,
    CameraCommand,
    CameraRuntime,
    ControlCommand,
    EngineCommand,
    LivePublisher,
    ManagedProcess,
    Supervisor,
)
from .synchronized import SyncMember

logger = logging.getLogger("vcam")

STATE_ENV = "VCAM_STATE_DIR"
DESCRIPTOR_VERSION = 1
MAX_EVENTS = 1000
MAX_OPERATIONS = 1000
MAX_LOG_RECORDS = 2000
MAX_PLANS = 16


# -- discovery files ----------------------------------------------------------


def state_dir() -> Path:
    """Private directory holding control tokens and endpoint descriptors."""
    override = os.environ.get(STATE_ENV)
    if override:
        base = Path(override).expanduser()
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "vcam"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "vcam"
    return base.absolute() / "control"


def config_key(config_path: Path) -> str:
    return hashlib.sha256(str(config_path.expanduser().resolve()).encode()).hexdigest()


def descriptor_path(config_path: Path) -> Path:
    return state_dir() / f"{config_key(config_path)}.json"


def token_path(config_path: Path) -> Path:
    return state_dir() / f"{config_key(config_path)}.token"


def lock_path(config_path: Path) -> Path:
    return state_dir() / f"{config_key(config_path)}.lock"


def _private_dir(path: Path, *, create: bool = True) -> None:
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise SupervisorError(
            "control state root must be a user-owned, non-writable-by-others directory"
        )
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SupervisorError("control state directory must be owner-only (0700)")


def _private_fd(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        os.close(fd)
        raise SupervisorError("control state files must be regular owner-only files")
    return fd


def _read_private(path: Path, maximum: int) -> str:
    with os.fdopen(_private_fd(path, os.O_RDONLY), "rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise SupervisorError("control state file exceeds size limit")
    return raw.decode("utf-8")


def read_token_file(path: Path) -> str:
    """Read a credential without accepting symlinks, other owners or public permissions."""
    try:
        value = _read_private(path, 4096).removesuffix("\n").removesuffix("\r")
    except (OSError, UnicodeError) as exc:
        raise SupervisorError("cannot read private control token file") from exc
    if (
        not value
        or not value.isascii()
        or not value.isprintable()
        or any(c.isspace() for c in value)
    ):
        raise SupervisorError("control token file must contain a single ASCII bearer token")
    return value


@contextlib.contextmanager
def config_ownership(config_path: Path) -> Iterator[None]:
    """Exclude other controlled owners and offline writers for this config identity."""
    try:
        _private_dir(state_dir())
        fd = _private_fd(lock_path(config_path), os.O_RDWR | os.O_CREAT)
    except OSError as exc:
        raise SupervisorError("cannot open the private control ownership lock") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SupervisorError("another vcam process controls this config") from exc
        yield
    finally:
        os.close(fd)


def _write_private(path: Path, text: str) -> None:
    _private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_descriptor(config_path: Path) -> dict[str, Any] | None:
    try:
        _private_dir(state_dir(), create=False)
        data = json.loads(_read_private(descriptor_path(config_path), 16384))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SupervisorError("cannot read control discovery descriptor") from exc
    if not isinstance(data, dict) or not all(
        isinstance(data.get(field), str)
        for field in ("instance_id", "endpoint", "config_path", "token_file")
    ):
        raise SupervisorError("invalid control discovery descriptor")
    pid = data.get("pid")
    ca = data.get("ca_file")
    if (
        type(pid) is not int
        or not 0 < pid < 2**31
        or not data["instance_id"]
        or not Path(data["token_file"]).is_absolute()
        or (ca is not None and (not isinstance(ca, str) or not Path(ca).is_absolute()))
    ):
        raise SupervisorError("invalid control discovery identity or credential location")
    if (
        type(data.get("version")) is not int
        or data["version"] != DESCRIPTOR_VERSION
        or data["config_path"] != str(config_path.resolve())
    ):
        raise SupervisorError("incompatible control descriptor or config identity")
    return data


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# -- errors -------------------------------------------------------------------


class ControlError(Exception):
    """An API-level failure, rendered as the JSON error envelope."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _not_found(port: int, name: str) -> ControlError:
    return ControlError(404, "camera_not_found", f"no camera {name!r} on port {port}")


# -- operations, events, logs ---------------------------------------------------


@dataclass
class Operation:
    id: str
    kind: str
    instance_id: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    config_persisted: bool = False
    applied: bool = False
    future: Future | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "instance_id": self.instance_id,
            "status": (
                "running"
                if self.status == "queued" and self.future and self.future.running()
                else self.status
            ),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "config_persisted": self.config_persisted,
            "applied": self.applied,
        }


class EventLog:
    """Bounded, sequenced event history; SSE cursors are ``<instance>:<seq>``."""

    def __init__(self, instance_id: str, maxlen: int = MAX_EVENTS) -> None:
        self.instance_id = instance_id
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._seq = 0
        self._cond = threading.Condition()

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        with self._cond:
            self._seq += 1
            self._events.append(
                {
                    "id": f"{self.instance_id}:{self._seq}",
                    "seq": self._seq,
                    "event": kind,
                    "time": time.time(),
                    "data": data,
                }
            )
            self._cond.notify_all()

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def parse_cursor(self, cursor: str | None) -> int:
        """Sequence after which to resume; raises when the cursor cannot resume."""
        if not cursor:
            return self.last_seq
        instance, _, raw = cursor.rpartition(":")
        if not raw.isascii() or not raw.isdigit() or len(raw) > 20:
            raise ControlError(400, "invalid_event_cursor", "invalid event sequence")
        if instance != self.instance_id:
            raise ControlError(
                409,
                "event_cursor_wrong_instance",
                "event cursor belongs to another vcam instance; refetch status",
            )
        seq = int(raw)
        with self._cond:
            oldest = self._events[0]["seq"] if self._events else self._seq + 1
            if seq > self._seq or (seq + 1 < oldest):
                raise ControlError(
                    409, "event_cursor_expired", "event cursor expired; refetch status"
                )
        return seq

    def since(self, seq: int, timeout: float = 0.0) -> list[dict[str, Any]]:
        with self._cond:
            if self._seq <= seq and timeout > 0:
                self._cond.wait(timeout)
            return [event for event in self._events if event["seq"] > seq]


class LogBuffer(logging.Handler):
    """Keeps the latest log records for ``GET /logs``."""

    def __init__(self, maxlen: int = MAX_LOG_RECORDS, redact: tuple[str, ...] = ()) -> None:
        super().__init__(level=logging.INFO)
        self.records: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._seq = 0
        self._redact = redact
        self._lock_records = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # a broken log call must not break the caller
            return
        message = redact_text(message)
        for secret in self._redact:
            message = message.replace(secret, "[REDACTED]")
        message = message.encode("utf-8", errors="replace")[:4096].decode("utf-8", errors="ignore")
        with self._lock_records:
            self._seq += 1
            self.records.append(
                {
                    "seq": self._seq,
                    "time": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )

    def tail(self, limit: int, since: int = 0) -> list[dict[str, Any]]:
        return self.page(limit, since)["records"]

    def page(
        self, limit: int, since: int = 0, level: str | None = None, resource: str | None = None
    ) -> dict[str, Any]:
        with self._lock_records:
            if since and (
                since > self._seq or (self.records and since + 1 < self.records[0]["seq"])
            ):
                raise ControlError(409, "log_cursor_expired", "refresh the log tail")
            matches = [
                r
                for r in self.records
                if r["seq"] > since
                and (not level or level == r["level"])
                and (not resource or r["message"].startswith((resource + ":", f"[{resource}] ")))
            ]
            page = matches[:limit] if since else matches[-limit:]
            cursor = page[-1]["seq"] if since and len(matches) > limit else self._seq
            return {"records": page, "cursor": cursor}


@dataclass
class _Plan:
    id: str
    base_revision: str
    owner_revision: str
    text: str | None
    """Proposed full document, committed on apply; None plans the file on disk."""
    target: CameraStack
    plan: ChangePlan
    created_at: float = field(default_factory=time.time)


_PathTarget = tuple[str, int | None, tuple[tuple[str, int | None], ...]]


class _PathReadiness:
    """Only accept observations for the same listener and publisher generations."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vcam-path-status")
        self.pending: dict[_PathTarget, Future[set[str] | None]] = {}
        self.observed: dict[_PathTarget, tuple[float, set[str] | None]] = {}

    @staticmethod
    def _read(url: str) -> set[str] | None:
        try:
            paths = MediaMTXClient(url, timeout=1.0).list_paths()
        except MediaMTXAPIError:
            return None
        return {name for name, path in paths.items() if path.get("ready")}

    def sample(self, targets: dict[int, _PathTarget]) -> dict[int, set[str] | None]:
        now = time.monotonic()
        active = set(targets.values())
        self.observed = {key: result for key, result in self.observed.items() if key in active}
        for key, future in list(self.pending.items()):
            if future.done():
                del self.pending[key]
                if key in active:
                    self.observed[key] = now, future.result()
        for key in sorted(targets.values(), key=lambda k: self.observed.get(k, (0, None))[0]):
            if len(self.pending) >= 2:
                break
            if key not in self.pending and now - self.observed.get(key, (0, None))[0] >= 0.5:
                self.pending[key] = self.pool.submit(self._read, key[0])
        return {port: self.observed.get(key, (0, None))[1] for port, key in targets.items()}

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)


# -- the controller -------------------------------------------------------------


class Controller:
    """Everything the HTTP handlers call; thread-safe."""

    def __init__(
        self,
        supervisor: Supervisor,
        config_path: Path | None,
        *,
        effective: Callable[[CameraStack], CameraStack],
        spec: ControlSpec,
        revision: str | None,
        declared: CameraStack | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.config_path = config_path.expanduser().resolve() if config_path is not None else None
        self.store = ConfigStore(self.config_path) if self.config_path is not None else None
        self._declared_text = ""
        if self.store is None:
            if declared is None:
                raise ValueError("inline control requires the declared launch stack")
            self._declared_text = _canonical_stack(declared)
            revision = hashlib.sha256(self._declared_text.encode()).hexdigest()
        if revision is None:
            raise ValueError("file-backed control requires a configuration revision")
        self.effective = effective
        self.spec = spec
        self.instance_id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.started_at = time.time()
        self.endpoint: str | None = None
        self.events = EventLog(self.instance_id)
        self.logs = LogBuffer(redact=(self.token,))
        self._lock = threading.RLock()
        self._mutation = threading.Lock()
        self.media_slots = threading.BoundedSemaphore(2)
        self._operations: collections.OrderedDict[str, Operation] = collections.OrderedDict()
        self._idempotency: collections.OrderedDict[str, tuple[str, str]] = collections.OrderedDict()
        self._plans: collections.OrderedDict[str, _Plan] = collections.OrderedDict()
        self.expected_revision = revision
        """Revision of the YAML the runtime was last told to converge to."""
        self.applied_revision: str | None = revision
        self.target_stack = supervisor.stack
        self._launch_declared = declared or self.target_stack
        self._snapshot: dict[str, Any] = {}
        self._readiness = _PathReadiness()
        self._states: dict[str, str] = {}
        self._engine_state = supervisor.engine_state
        self._ownership: contextlib.AbstractContextManager[None] | None = None
        self.token_file = spec.token_file or (
            token_path(self.config_path)
            if self.config_path
            else state_dir() / f"{self.instance_id}.token"
        )

    # -- discovery ------------------------------------------------------------

    def claim_ownership(self) -> None:
        if self._ownership is not None:
            return
        ownership = (
            config_ownership(self.config_path) if self.config_path else contextlib.nullcontext()
        )
        ownership.__enter__()
        try:
            if self.spec.token_file is None and not os.path.lexists(self.token_file):
                _write_private(self.token_file, self.token + "\n")
            self.token = read_token_file(self.token_file)
            self.logs = LogBuffer(redact=(self.token,))
            self._ownership = ownership
        finally:
            if self._ownership is None:
                ownership.__exit__(None, None, None)

    def publish_discovery(self, endpoint: str, ca_file: Path | None = None) -> None:
        """Publish only after binding, authenticating and starting the HTTP listener."""
        self.claim_ownership()
        self.endpoint = endpoint
        if self.config_path is None:
            # No local YAML identity to discover; clients use the printed endpoint/token file.
            return
        descriptor = {
            "version": DESCRIPTOR_VERSION,
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "endpoint": endpoint,
            "config_path": str(self.config_path),
            "token_file": str(self.token_file),
            "ca_file": str(ca_file) if ca_file is not None else None,
            "started_at": self.started_at,
        }
        _write_private(descriptor_path(self.config_path), json.dumps(descriptor, indent=2))

    def withdraw_discovery(self) -> None:
        self._readiness.close()
        if self._ownership is None:
            return
        try:
            if self.config_path is None:
                if self.spec.token_file is None:
                    self.token_file.unlink(missing_ok=True)
                return
            descriptor = read_descriptor(self.config_path)
            if descriptor is not None and descriptor.get("instance_id") == self.instance_id:
                descriptor_path(self.config_path).unlink(missing_ok=True)
        finally:
            self._ownership.__exit__(None, None, None)
            self._ownership = None

    def check_token(self, presented: str) -> bool:
        return secrets.compare_digest(presented.encode(), self.token.encode())

    # -- snapshots (monitor thread) ---------------------------------------------

    def tick(self, supervisor: Supervisor) -> None:
        """Runs on the monitor thread: publish an immutable runtime snapshot."""
        generations: dict[int, list[tuple[str, int | None]]] = collections.defaultdict(list)
        for runtime in supervisor.runtimes:
            generations[runtime.instance.rtsp_port].append(
                (runtime.generation, runtime.process.pid)
            )
        ready = self._readiness.sample(
            {
                instance.rtsp_port: (
                    instance.api_url,
                    server.pid,
                    tuple(generations[instance.rtsp_port]),
                )
                for instance, server in zip(supervisor.instances, supervisor.servers, strict=True)
                if server.running
            }
        )
        stack = supervisor.stack
        cameras = []
        for runtime in supervisor.runtimes:
            port, name = camera_key(stack, runtime.camera)
            cameras.append(
                {
                    "port": port,
                    "name": name,
                    "state": _camera_state(runtime, ready.get(port)),
                    "mode": runtime.mode.value,
                    "running": runtime.process.running,
                    "generation": runtime.generation,
                    "pid": runtime.process.pid,
                    "restarts": runtime.process.restarts,
                    "run_intent": "stopped" if (port, name) in supervisor.held_keys else "running",
                    **(
                        {
                            "manual_restarts": runtime.process.manual_restarts,
                            "retry_in": max(0, runtime.process.retry_at - time.monotonic()),
                            "gave_up": runtime.process.gave_up,
                        }
                        if isinstance(runtime.process, ManagedProcess)
                        else {}
                    ),
                    "last_exit_code": runtime.process.last_exit_code,
                    **(
                        {"simulation_state": runtime.scheduler.state_label}
                        if runtime.scheduler is not None
                        else {}
                    ),
                }
            )
        snapshot = {
            "engine": supervisor.engine_state,
            "stack": stack,
            "servers": [
                {
                    "port": instance.rtsp_port,
                    "running": server.running,
                    "pid": server.pid,
                    "restarts": server.restarts,
                }
                for instance, server in zip(supervisor.instances, supervisor.servers, strict=False)
            ],
            "cameras": cameras,
            "replays": [
                {
                    "name": replay.replay.name,
                    "port": replay.replay.port,
                    "url": replay.read_url,
                    "state": "running" if replay.process.running else "failed",
                    "pid": replay.process.pid,
                    "restarts": replay.process.restarts,
                }
                for replay in supervisor.replays
            ],
            "sync_groups": [
                {
                    "name": group.name,
                    "members": [member.name for member in group.members],
                    "epoch_ns": group.epoch_ns or None,
                    "lead_frames": group.lead,
                }
                for group in supervisor.sync_groups
            ],
            "updated_at": time.time(),
        }
        with self._lock:
            self._snapshot = snapshot
        self._emit_changes(snapshot)

    def _emit_changes(self, snapshot: dict[str, Any]) -> None:
        if snapshot["engine"] != self._engine_state:
            self._engine_state = snapshot["engine"]
            self.events.publish("engine", {"state": snapshot["engine"]})
        states = {f"{c['port']}/{c['name']}": c["state"] for c in snapshot["cameras"]}
        for key, state in states.items():
            if self._states.get(key) != state:
                port, _, name = key.partition("/")
                self.events.publish("camera", {"port": int(port), "name": name, "state": state})
        for key in self._states.keys() - states.keys():
            port, _, name = key.partition("/")
            self.events.publish("camera", {"port": int(port), "name": name, "state": "removed"})
        self._states = states

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot

    def _admission_snapshot(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        if not snapshot:
            raise ControlError(503, "engine_starting", "wait for /ready before sending commands")
        return snapshot

    # -- reads --------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        snap = self.snapshot()
        cameras = snap.get("cameras", [])
        desired = self._desired_revision()
        return {
            "instance_id": self.instance_id,
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "engine": {"state": snap.get("engine", self.supervisor.engine_state)},
            "config": {
                **self._persistence(desired),
                "revision": desired,
                "expected_revision": self.expected_revision,
                "applied_revision": self.applied_revision,
                "pending_reload": desired != self.expected_revision,
                "converged": desired == self.applied_revision == self.expected_revision,
            },
            "counts": {
                # Declared totals: a stopped engine still owns its cameras.
                "cameras": len(self.target_stack.cameras),
                "enabled": len(self.target_stack.enabled_cameras),
                "running": sum(c["state"] == "running" for c in cameras),
                "replays": len(self.target_stack.replays),
            },
            "servers": snap.get("servers", []),
            "updated_at": snap.get("updated_at"),
        }

    def ready(self) -> tuple[bool, dict[str, Any]]:
        """Whether the owner loop is alive and accepting commands.

        A stopped engine or an empty stack is still ready: it can take commands.
        """
        snap = self.snapshot()
        updated = snap.get("updated_at")
        fresh = updated is not None and time.time() - updated < 10.0
        accepting = fresh and not self.supervisor.stopping and not self.supervisor.commands.full()
        return accepting, {
            "ready": accepting,
            "engine": snap.get("engine", self.supervisor.engine_state),
        }

    def health(self) -> tuple[bool, dict[str, Any]]:
        """Stream health: engine running and every enabled camera publishing."""
        snap = self.snapshot()
        cameras, _ = self.cameras()
        waiting = [
            {"kind": "camera", "port": c["port"], "name": c["name"], "state": c["state"]}
            for c in cameras
            if c["enabled"] and c["state"] not in {"running", "stopped"}
        ]
        waiting.extend(
            {"kind": "replay", "port": r["port"], "name": r["name"], "state": r["state"]}
            for r in self.replays()
            if r["enabled"] and r["state"] != "running"
        )
        ok = (
            snap.get("engine") == "running"
            and not waiting
            and all(s["running"] for s in snap.get("servers", []))
        )
        return ok, {"ready": ok, "engine": snap.get("engine"), "waiting": waiting}

    def _desired_revision(self) -> str | None:
        if self.store is None:
            return self.expected_revision
        try:
            return self.store.disk_revision()
        except ConfigError:
            return None

    def _persistence(self, revision: str | None) -> dict[str, Any]:
        path = self.config_path
        if path is None:
            return {"path": None, "persistence": "ephemeral", "disk_revision": None}
        try:
            writable = all(
                p.stat().st_mode & 0o222 and os.access(p, os.W_OK) for p in (path, path.parent)
            )
        except OSError:
            writable = False
        return {
            "path": str(path),
            "persistence": "file" if writable else "read-only",
            "disk_revision": revision,
        }

    def _camera_view(self, stack: CameraStack, camera: CameraSpec, live: dict[str, Any] | None):
        port, name = camera_key(stack, camera)
        if not camera.enabled:
            state = "disabled"
        elif live is not None:
            state = live["state"]
        else:
            state = "stopped" if self.snapshot().get("engine") != "running" else "pending"
        view = {
            "port": port,
            "name": name,
            "enabled": camera.enabled,
            "state": state,
            "url": stack.read_url(camera, with_credentials=False),
            "source": display_source(camera.source),
            "live": camera.is_live,
            "sync_group": camera.sync_group,
            "simulation": camera.simulation.mode.value,
            "spec": _redacted_spec(camera),
        }
        if live is not None:
            view["runtime"] = {k: v for k, v in live.items() if k not in {"port", "name", "state"}}
        return view

    def cameras(self) -> tuple[list[dict[str, Any]], str | None]:
        stack = self.target_stack
        live = {(c["port"], c["name"]): c for c in self.snapshot().get("cameras", [])}
        return (
            [
                self._camera_view(stack, camera, live.get(camera_key(stack, camera)))
                for camera in stack.cameras
            ],
            self._desired_revision(),
        )

    def camera(self, port: int, name: str) -> tuple[dict[str, Any], str | None]:
        cameras, revision = self.cameras()
        for camera in cameras:
            if camera["port"] == port and camera["name"] == name:
                return camera, revision
        raise _not_found(port, name)

    def replays(self) -> list[dict[str, Any]]:
        live = {(r["port"], r["name"]): r for r in self.snapshot().get("replays", [])}
        stack = self.target_stack
        return [
            {
                "name": replay.name,
                "port": replay.port,
                "enabled": replay.enabled,
                "url": stack.replay_url(replay, with_credentials=False),
                "state": live.get((replay.port, replay.name), {}).get(
                    "state", "disabled" if not replay.enabled else "stopped"
                ),
            }
            for replay in stack.replays
        ]

    def sync_groups(self) -> list[dict[str, Any]]:
        return list(self.snapshot().get("sync_groups", []))

    def urls(self) -> list[str]:
        stack = self.target_stack
        return [stack.read_url(c, with_credentials=False) for c in stack.enabled_cameras] + [
            stack.replay_url(r, with_credentials=False) for r in stack.enabled_replays
        ]

    def config(self) -> dict[str, Any]:
        """Declared (YAML) and effective (with CLI overrides) stacks, redacted.

        The raw document is never returned: it can hold credentials.
        """
        document = self._read()
        try:
            declared_stack = parse_stack_text(document.text, self.config_path, allow_empty=True)
            effective_stack = self.effective(declared_stack.model_copy(deep=True))
        except (ConfigError, ValueError) as exc:
            raise ControlError(422, "invalid_config", str(exc)) from exc
        declared = declared_stack.model_dump(mode="json")
        effective = effective_stack.model_dump(mode="json")
        return {
            **self._persistence(document.revision),
            "revision": document.revision,
            "expected_revision": self.expected_revision,
            "applied_revision": self.applied_revision,
            "pending_reload": document.revision != self.expected_revision,
            "declared": redact(declared),
            "effective": redact(effective),
            "provenance": {
                "declared": "file" if self.store else "inline",
                "overridden": _differences(declared, effective),
            },
        }

    # -- operations ---------------------------------------------------------------

    def operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ControlError(404, "operation_not_found", "unknown operation")
            return operation.to_dict()

    def _reuse(self, idempotency_key: str | None, kind: str, payload: Any) -> dict[str, Any] | None:
        """The operation an earlier identical request created, if any.

        A key replayed with a different request is an error, never a silent
        reuse: the caller would otherwise believe a change it never made.
        """
        if not idempotency_key:
            return None
        with self._lock:
            entry = self._idempotency.get(idempotency_key)
            operation = self._operations.get(entry[0]) if entry else None
        if entry is None or operation is None:
            return None
        if entry[1] != _fingerprint(kind, payload):
            raise ControlError(
                422,
                "idempotency_key_reused",
                "Idempotency-Key was already used for a different request",
            )
        return operation.to_dict()

    def _new_operation(self, kind: str) -> Operation:
        return Operation(id=uuid.uuid4().hex, kind=kind, instance_id=self.instance_id)

    def _submit(
        self,
        operation: Operation,
        command: ControlCommand,
        idempotency_key: str | None,
        payload: Any,
        on_success: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self._admission_snapshot()
        try:
            future = self.supervisor.submit(command)
        except queue.Full as exc:
            raise ControlError(429, "busy", "too many queued commands; retry later") from exc
        except SupervisorError as exc:
            raise ControlError(503, "shutting_down", str(exc)) from exc
        with self._lock:
            operation.future = future
            self._operations[operation.id] = operation
            if len(self._operations) > MAX_OPERATIONS:
                self._operations.popitem(last=False)
            if idempotency_key:
                self._idempotency[idempotency_key] = (
                    operation.id,
                    _fingerprint(operation.kind, payload),
                )
                if len(self._idempotency) > MAX_OPERATIONS:
                    self._idempotency.popitem(last=False)
        self.events.publish(
            "operation", {"id": operation.id, "kind": operation.kind, "status": "queued"}
        )

        def done(result: Future) -> None:
            if result.cancelled():
                self._finish(operation, error=ControlError(503, "cancelled", "operation cancelled"))
                return
            exc = result.exception()
            if exc is None:
                if on_success is not None:
                    on_success()
                self._finish(operation, result=result.result())
            else:
                self._finish(operation, error=exc)

        future.add_done_callback(done)
        return operation.to_dict()

    def _finish(
        self,
        operation: Operation,
        *,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            operation.finished_at = time.time()
            operation.future = None
            if error is None:
                operation.status = "succeeded"
                operation.result = {**(operation.result or {}), **(result or {})}
                operation.applied = True
            else:
                operation.status = (
                    "cancelled"
                    if isinstance(error, ControlError) and error.code == "cancelled"
                    else "failed"
                )
                operation.error = _error_dict(error)
        self.events.publish(
            "operation",
            {"id": operation.id, "kind": operation.kind, "status": operation.status},
        )

    # -- engine and camera actions ----------------------------------------------------

    def engine_action(
        self, action: str, *, allow_disruption: bool, idempotency_key: str | None
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ControlError(422, "invalid_action", f"unknown engine action {action!r}")
        _require_bool(allow_disruption, "allow_disruption")
        kind = f"engine.{action}"
        payload = {"allow_disruption": allow_disruption}
        with self._mutation:
            if (reused := self._reuse(idempotency_key, kind, payload)) is not None:
                return reused
            if action != "start" and not allow_disruption:
                raise ControlError(
                    409,
                    "disruption_not_acknowledged",
                    f"engine {action} interrupts every stream; set allow_disruption",
                )
            return self._submit(
                self._new_operation(kind), EngineCommand(action), idempotency_key, payload
            )

    def camera_action(
        self, port: int, name: str, action: str, *, idempotency_key: str | None
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ControlError(422, "invalid_action", f"unknown camera action {action!r}")
        kind = f"camera.{action}"
        payload = {"port": port, "name": name}
        with self._mutation:
            if (reused := self._reuse(idempotency_key, kind, payload)) is not None:
                return reused
            camera, _ = self.camera(port, name)
            if not camera["enabled"]:
                raise ControlError(409, "camera_disabled", "enable the camera before acting on it")
            if camera["sync_group"]:
                raise ControlError(
                    409, "group_action_required", "synchronized cameras run as a group"
                )
            if self.snapshot().get("engine") == "stopped":
                raise ControlError(
                    409, "engine_stopped", "start orchestration before acting on a camera"
                )
            return self._submit(
                self._new_operation(kind),
                CameraCommand((port, name), action),
                idempotency_key,
                payload,
            )

    # -- configuration mutations ----------------------------------------------------

    def _read(self) -> ConfigDocument:
        try:
            return self.store.read() if self.store else ConfigDocument.parse(self._declared_text)
        except ConfigError as exc:
            raise ControlError(500, "config_unreadable", str(exc)) from exc

    def _validate(self, text: str) -> CameraStack:
        try:
            desired = parse_stack_text(text, self.config_path, allow_empty=True)
            return self.effective(desired)
        except ConfigError as exc:
            raise ControlError(422, "invalid_config", str(exc)) from exc
        except ValidationError as exc:
            raise ControlError(422, "invalid_config", format_validation_error(exc)) from exc
        except ValueError as exc:
            raise ControlError(422, "invalid_config", str(exc)) from exc

    def _check_precondition(self, document: ConfigDocument, if_match: str | None) -> None:
        _match(if_match, document.revision)
        if document.revision != self.expected_revision:
            raise ControlError(
                409,
                "reload_required",
                "the config file was edited outside the API; plan and apply it first",
                {"revision": document.revision},
            )

    def _store_plan(self, plan: _Plan) -> dict[str, Any]:
        if not plan.plan.restart_required:
            self._admit(plan.target, plan.plan)
        with self._lock:
            self._plans[plan.id] = plan
            while len(self._plans) > MAX_PLANS:
                self._plans.popitem(last=False)
        stacks = (self.target_stack, plan.target)
        affected = set(plan.plan.added + plan.plan.updated + plan.plan.removed)
        if plan.plan.disruptive:
            affected.update(camera_key(s, c) for s in stacks for c in s.enabled_cameras)
        return {
            "plan_id": plan.id,
            "base_revision": plan.base_revision,
            "proposed": plan.text is not None,
            "affected": [{"port": port, "name": name} for port, name in sorted(affected)],
            "reset_sync_groups": sorted(
                {
                    c.sync_group
                    for s in stacks
                    for c in s.enabled_cameras
                    if plan.plan.disruptive and c.sync_group
                }
            ),
            "restart_replays": [
                {"port": port, "name": name}
                for port, name in sorted(
                    {
                        (r.port, r.name)
                        for s in stacks
                        for r in s.enabled_replays
                        if plan.plan.disruptive
                    }
                )
            ],
            **plan.plan.to_dict(),
        }

    def _admit(self, target: CameraStack, plan: ChangePlan) -> MediaAdmission:
        if not self.media_slots.acquire(blocking=False):
            raise ControlError(429, "admission_busy", "all media inspection workers are busy")
        try:
            snap = self._admission_snapshot()
            owned = {s["port"] for s in snap.get("servers", []) if s["running"]}
            owned.update(r["port"] for r in snap.get("replays", []) if r["pid"])
            try:
                check_listeners(
                    target, owned, urlsplit(self.endpoint).port if self.endpoint else self.spec.port
                )
            except SupervisorError as exc:
                raise ControlError(409, "listener_unavailable", str(exc)) from exc
            return preflight(target, plan, self.supervisor.ffprobe)
        except (OSError, VcamError, ValueError) as exc:
            # Diagnostics from codecs, capture parsers or paths can contain credentials.
            raise ControlError(
                422,
                "media_preflight_failed",
                "media admission failed; check that enabled sources exist on the engine host "
                "and contain a supported video track or replay",
            ) from exc
        finally:
            self.media_slots.release()

    def _edit_cameras(
        self,
        kind: str,
        payload: Any,
        if_match: str | None,
        idempotency_key: str | None,
        edit: Callable[[CommentedMap, CameraStack], None],
    ) -> dict[str, Any]:
        with self._mutation:
            if (reused := self._reuse(idempotency_key, kind, payload)) is not None:
                return reused
            document = self._read()
            self._check_precondition(document, if_match)
            current = self._validate(document.text)
            try:
                edit(document.data, current)
                text = ConfigStore.preview(document)
            except ConfigMigrationRequired as exc:
                raise ControlError(409, "config_migration_required", str(exc)) from exc
            target = self._validate(text)
            plan = plan_changes(self.target_stack, target)
            if plan.disruptive:
                proposal = _Plan(
                    uuid.uuid4().hex, document.revision, self.expected_revision, text, target, plan
                )
                raise ControlError(
                    409,
                    "full_apply_required",
                    "this change restarts the engine; review the plan and apply it explicitly",
                    self._store_plan(proposal),
                )
            return self._commit_and_apply(
                kind,
                document.revision,
                text,
                target,
                plan,
                False,
                idempotency_key,
                payload,
                owner_revision=document.revision,
            )

    def _commit_and_apply(
        self,
        kind: str,
        base_revision: str,
        text: str | None,
        target: CameraStack,
        plan: ChangePlan,
        full: bool,
        idempotency_key: str | None,
        payload: Any,
        *,
        owner_revision: str,
    ) -> dict[str, Any]:
        if plan.restart_required:
            raise ControlError(
                409,
                "restart_required",
                "change startup-only settings locally and restart the daemon",
                {"reasons": list(plan.restart_required)},
            )
        admission = self._admit(target, plan)
        if self.store is None and text is not None:
            text = _canonical_stack(parse_stack_text(text, None, allow_empty=True))
        revision = hashlib.sha256(text.encode()).hexdigest() if text is not None else base_revision
        operation = self._new_operation(kind)
        operation.result = {"revision": revision, "plan": plan.to_dict()}

        def record_desired() -> None:
            with self._lock:
                self.expected_revision = revision
                self.target_stack = target
                operation.config_persisted = self.store is not None and text is not None
            self.events.publish("config", {"revision": revision})

        def commit_on_owner() -> None:
            # Queue admission and the final version check precede every durable side effect.
            for path, stamp in admission.stamps.items():
                try:
                    unchanged = file_stamp(path) == stamp
                except (SupervisorError, OSError):
                    unchanged = False
                if not unchanged:
                    raise ControlError(409, "source_changed", "source changed after preflight")
            if self.expected_revision != owner_revision:
                raise ControlError(
                    412, "revision_mismatch", "an earlier operation changed the config"
                )
            if self.store is None:
                _match(base_revision, self.expected_revision)
                if text is not None:
                    self._declared_text = text
            elif text is None:
                _match(base_revision, self.store.disk_revision())
            else:
                try:
                    self.store.commit(text, expected_revision=base_revision)
                except ConfigRevisionConflict as exc:
                    raise ControlError(
                        412,
                        "revision_mismatch",
                        "configuration changed; read it again",
                        {"revision": exc.actual_revision},
                    ) from exc
                except ConfigWriteError as exc:
                    if exc.config_persisted:
                        record_desired()
                    raise ControlError(
                        500 if exc.config_persisted else 409,
                        "config_write_failed",
                        str(exc),
                        {"config_persisted": exc.config_persisted},
                    ) from exc
            record_desired()

        def applied() -> None:
            self.applied_revision = revision

        renamed = None
        if kind == "camera.update" and len(plan.removed) == len(plan.added) == 1:
            renamed = (plan.removed[0], plan.added[0])
        return self._submit(
            operation,
            ApplyCommand(
                target,
                full=full,
                before_apply=commit_on_owner,
                renamed=renamed,
                admission=admission,
            ),
            idempotency_key,
            payload,
            applied,
        )

    def create_camera(
        self, body: dict[str, Any], *, if_match: str | None, idempotency_key: str | None
    ) -> dict[str, Any]:
        entry = _camera_entry(body)

        def edit(data: CommentedMap, _current: CameraStack) -> None:
            camera_nodes(data).append(camera_fields(data, entry))

        return self._edit_cameras("camera.create", body, if_match, idempotency_key, edit)

    def update_camera(
        self,
        port: int,
        name: str,
        patch: dict[str, Any],
        *,
        if_match: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            raise ControlError(422, "invalid_request", "send a JSON merge patch object")

        def edit(data: CommentedMap, current: CameraStack) -> None:
            cameras = camera_nodes(data)
            index = _camera_index(current, port, name)
            _merge_patch(cameras[index], camera_fields(data, patch))

        return self._edit_cameras(
            "camera.update",
            {"port": port, "name": name, "patch": patch},
            if_match,
            idempotency_key,
            edit,
        )

    def delete_camera(
        self, port: int, name: str, *, if_match: str | None, idempotency_key: str | None
    ) -> dict[str, Any]:
        def edit(data: CommentedMap, current: CameraStack) -> None:
            del camera_nodes(data)[_camera_index(current, port, name)]

        return self._edit_cameras(
            "camera.delete", {"port": port, "name": name}, if_match, idempotency_key, edit
        )

    def plan(self, text: str | None, *, if_match: str | None) -> dict[str, Any]:
        """Preview converging to the file on disk, or to a proposed ``text``."""
        document = self._read()
        if text is not None:
            _match(if_match, document.revision)
        owner_revision = self.expected_revision
        target, plan = self._document_plan(text if text is not None else document.text)
        return self._store_plan(
            _Plan(uuid.uuid4().hex, document.revision, owner_revision, text, target, plan)
        )

    def _document_plan(self, text: str) -> tuple[CameraStack, ChangePlan]:
        target = self._validate(text)
        plan = plan_changes(self.target_stack, target)
        declared = parse_stack_text(text, self.config_path, allow_empty=True)
        # A CLI override must not mask an unsafe edit to the next daemon invocation.
        if (
            declared.control != self._launch_declared.control
            or declared.server.ntp_server != self._launch_declared.server.ntp_server
        ) and not plan.restart_required:
            plan = replace(plan, restart_required=("control and clock settings are startup-only",))
        return target, plan

    def apply(
        self,
        plan_id: str,
        *,
        allow_disruption: bool,
        if_match: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Commit (when proposed) and converge to a previously reviewed plan."""
        _require_bool(allow_disruption, "allow_disruption")
        payload = {"plan_id": plan_id, "allow_disruption": allow_disruption}
        with self._mutation:
            if (reused := self._reuse(idempotency_key, "config.apply", payload)) is not None:
                return reused
            document = self._read()
            _match(if_match, document.revision)
            with self._lock:
                plan = self._plans.get(plan_id)
            if plan is None:
                raise ControlError(404, "plan_not_found", "unknown or expired plan; plan again")
            if (
                document.revision != plan.base_revision
                or self.expected_revision != plan.owner_revision
            ):
                raise ControlError(
                    409, "plan_stale", "configuration changed since this plan; plan again"
                )
            if plan.plan.disruptive and not allow_disruption:
                raise ControlError(
                    409,
                    "disruption_not_acknowledged",
                    "this plan restarts the engine; set allow_disruption",
                    plan.plan.to_dict(),
                )
            accepted = self._commit_and_apply(
                "config.apply",
                plan.base_revision,
                plan.text,
                plan.target,
                plan.plan,
                plan.plan.disruptive,
                idempotency_key,
                payload,
                owner_revision=plan.owner_revision,
            )
            with self._lock:
                self._plans.pop(plan_id, None)
            return accepted

    def reload(self) -> dict[str, Any]:
        """Preview converging to the file as it is on disk now.

        Reloading never changes the runtime by itself: callers apply the
        returned plan with ``apply(plan_id, if_match=base_revision)``.
        """
        if self.store is None:
            raise ControlError(
                409, "ephemeral_config", "inline stacks have no configuration file to reload"
            )
        with self._mutation:
            document = self._read()
            owner_revision = self.expected_revision
            target, plan = self._document_plan(document.text)
            return self._store_plan(
                _Plan(uuid.uuid4().hex, document.revision, owner_revision, None, target, plan)
            )


# -- helpers ----------------------------------------------------------------------


def _canonical_stack(stack: CameraStack) -> str:
    return json.dumps(stack.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _fingerprint(kind: str, payload: Any) -> str:
    raw = json.dumps([kind, payload], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _require_bool(value: Any, name: str) -> None:
    if not isinstance(value, bool):
        raise ControlError(422, "invalid_request", f"`{name}` must be a JSON boolean")


def _match(if_match: str | None, revision: str) -> None:
    """Enforce ``If-Match`` against the current config revision."""
    if if_match is None:
        raise ControlError(
            428,
            "precondition_required",
            "send If-Match with the config revision",
            {"revision": revision},
        )
    if if_match.strip().strip('"') != revision:
        raise ControlError(
            412,
            "revision_mismatch",
            "configuration changed; read it again",
            {"revision": revision},
        )


def redact(value: Any, key: str = "") -> Any:
    """Copy of a dumped model with secrets masked (passwords, URL credentials)."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key in {"password", "secret", "token"} and value:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    return value


def _differences(a: Any, b: Any, prefix: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(a.keys() | b.keys(), key=str):
            out += _differences(a.get(key), b.get(key), f"{prefix}.{key}" if prefix else key)
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _differences(x, y, f"{prefix}[{index}]")
        return out
    return [] if a == b else [prefix]


def _camera_state(runtime: CameraRuntime, ready: set[str] | None) -> str:
    process = runtime.process
    if isinstance(process, ManagedProcess):
        if process.held:
            return "stopped"
        if process.suspended:
            return "suspended"
    if isinstance(process, LivePublisher) and not process.running:
        return process.state
    if process.running:
        if ready is None:
            return "unknown"
        return "running" if runtime.camera.path_suffix() in ready else "starting"
    if isinstance(process, SyncMember):
        return "failed"
    return "failed" if process.gave_up else "restarting"


def _redacted_spec(camera: CameraSpec) -> dict[str, Any]:
    spec = camera.model_dump(mode="json", exclude_defaults=True)
    spec["name"] = camera.name
    spec["source"] = display_source(camera.source)
    return spec


def _error_dict(error: BaseException) -> dict[str, Any]:
    if isinstance(error, ControlError):
        return {"code": error.code, "message": error.message, "details": error.details}
    if isinstance(error, ApplyError):
        return {
            "code": "apply_failed",
            "message": redact_text(str(error)),
            "details": redact(error.failures),
        }
    if isinstance(error, LookupError):
        return {"code": "not_found", "message": str(error.args[0] if error.args else error)}
    return {"code": "failed", "message": redact_text(str(error))}


def _camera_entry(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ControlError(422, "invalid_request", "send a camera object")
    try:
        camera = CameraSpec.model_validate(body)
    except ValidationError as exc:
        raise ControlError(422, "invalid_camera", format_validation_error(exc)) from exc
    entry = camera.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    return {"name": camera.name, "source": str(camera.source), **entry}


def _camera_index(stack: CameraStack, port: int, name: str) -> int:
    key: CameraKey = (port, name)
    for index, camera in enumerate(stack.cameras):
        if camera_key(stack, camera) == key:
            return index
    raise _not_found(port, name)


def _merge_patch(target: CommentedMap, patch: dict[str, Any]) -> None:
    """RFC 7396 merge patch, preserving the YAML nodes (and comments) it keeps."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), CommentedMap):
            _merge_patch(target[key], value)
        elif isinstance(value, dict):
            target[key] = CommentedMap(value)
        else:
            target[key] = value


@contextlib.contextmanager
def capture_logs(controller: Controller) -> Iterator[None]:
    target = logging.getLogger("vcam")
    target.addHandler(controller.logs)
    try:
        yield
    finally:
        target.removeHandler(controller.logs)
