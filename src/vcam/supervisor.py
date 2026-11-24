"""Run and supervise the MediaMTX servers and their ffmpeg publishers."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .errors import ProbeError, SupervisorError
from .ffmpeg import build_publish_command, effective_mode
from .mediamtx import (
    UDP_BLOCK_SIZE,
    MediaMTXAPIError,
    MediaMTXClient,
    ServerInstance,
    plan_instances,
    write_server_config,
)
from .models import (
    CameraSpec,
    CameraStack,
    ReplaySpec,
    SimulationMode,
    SimulationSpec,
    StreamMode,
)
from .probe import MediaInfo, probe, try_probe
from .reconcile import CameraKey, MediaAdmission, camera_key, enabled_by_key, plan_changes
from .service import vcam_command
from .sources import display_source
from .synchronized import SyncGroup, SyncMember, admit_source

logger = logging.getLogger("vcam")

#: Environment variable carrying the reader password to a spawned `vcam replay`.
REPLAY_PASSWORD_ENV = "VCAM_REPLAY_PASSWORD"

#: Bound on queued control commands; submitters get queue.Full beyond it.
COMMAND_QUEUE_SIZE = 32

BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
STABLE_RUNTIME = 20.0  # a publisher alive this long resets its backoff


@dataclass
class ManagedProcess:
    """A supervised child process with exponential backoff restart strategy.

    Monitors a child process and automatically restarts it on failure with
    exponential backoff (1s, 2s, 4s, ..., max 30s). Successful runtimes >= 20s
    reset the backoff counter, allowing new failures to restart quickly.

    Lifecycle:
        - start() launches the child and increments consecutive_failures on error.
        - If a process runs >= STABLE_RUNTIME (20s), consecutive_failures resets.
        - If consecutive_failures exceeds a threshold (configured elsewhere),
          gave_up is set and restart is abandoned.
        - The monitor thread checks retry_at deadlines and calls start() again.
        - When suspended=True (for simulation-driven stops), the monitor leaves
          the process alone; the simulation scheduler owns its stop/start cycle.

    Attributes:
        name: Human-readable process name for logging.
        command: Command argv to execute.
        kind: "server" or "publisher" (for logging).
        env: Optional extra environment variables (merged over os.environ).
        process: The running subprocess.Popen, or None if not running.
        restarts: Total number of restart attempts.
        consecutive_failures: Failures since the last successful run.
        started_at: Wall-clock time when the process started.
        last_exit_code: Exit code from the last run.
        retry_at: Wall-clock time to attempt the next restart.
        gave_up: If True, restart has been abandoned.
        suspended: If True, this process is paused by a simulation scheduler.
    """

    name: str
    command: list[str]
    kind: str  # "server" or "publisher"
    env: dict[str, str] | None = None
    """Extra environment for the child, merged over the supervisor's own.

    Used for secrets: argv is world-readable through /proc/<pid>/cmdline.
    """
    process: subprocess.Popen | None = None
    restarts: int = 0
    restart_budget_origin: int = 0
    manual_restarts: int = 0
    consecutive_failures: int = 0
    started_at: float | None = None
    last_exit_code: int | None = None
    retry_at: float = 0.0
    gave_up: bool = False
    suspended: bool = False
    """When set, the monitor leaves this process alone: a simulation scheduler
    owns its stop/start cycle, so stops are planned rather than failures."""
    held: bool = False
    """Stopped on request through the control API; not restarted until started
    again. Transient: never persisted; the supervisor re-applies it across
    engine stop/start by camera key."""
    before_start: Callable[[], object] | None = field(default=None, repr=False)
    """Runs before every spawn (including monitor restarts), while the child is down."""
    _reader: threading.Thread | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def start(self) -> bool:
        if self.running:
            return True  # already up (e.g. the monitor restarted it first)
        logger.debug("%s: %s", self.name, " ".join(display_source(arg) for arg in self.command))
        try:
            if self.before_start is not None:
                self.before_start()
            # Children stay in vcam's process group so that, if vcam itself is
            # killed, a service manager (launchd, systemd) reaps them with it
            # instead of leaving orphans holding the RTSP/API ports.
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env={**os.environ, **self.env} if self.env else None,
            )
        except OSError as exc:
            logger.error("%s: failed to start: %s", self.name, exc)
            self.process = None
            self.consecutive_failures += 1
            self.retry_at = time.monotonic() + self.backoff()
            return False

        self.started_at = time.monotonic()
        self._reader = threading.Thread(target=self._pump_output, args=(self.process,), daemon=True)
        self._reader.start()
        logger.info("%s: started (pid %s)", self.name, self.process.pid)
        return True

    def _pump_output(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", errors="replace").rstrip()
            for arg in self.command:
                if display_source(arg) != arg:
                    line = line.replace(arg, display_source(arg))
            if line:
                logger.info("[%s] %s", self.name, line)
        stream.close()

    def backoff(self) -> float:
        return min(BACKOFF_BASE * (2 ** max(self.consecutive_failures - 1, 0)), BACKOFF_MAX)

    def note_exit(self, code: int | None, *, planned: bool = False) -> None:
        uptime = time.monotonic() - self.started_at if self.started_at else 0.0
        self.last_exit_code = code
        self.process = None
        if planned:
            # A scheduled simulation stop is not a failure: it must not feed the
            # restart backoff, nor burn part of the --max-restarts budget.
            self.consecutive_failures = 0
            return
        if uptime >= STABLE_RUNTIME:
            self.consecutive_failures = 1
        else:
            self.consecutive_failures += 1
        self.retry_at = time.monotonic() + self.backoff()

    def stop(self, timeout: float = 5.0) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            logger.info("%s: stopping", self.name)
            try:
                self.process.terminate()
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("%s: did not exit, killing", self.name)
                self.process.kill()
                self.process.wait()
            except OSError:
                pass
        self.process = None


class LivePublisher(ManagedProcess):
    """Probe asynchronously before connecting; outages do not spend the restart budget."""

    def __init__(
        self,
        camera: CameraSpec,
        target_url: str,
        *,
        ffmpeg: str,
        ffprobe: str,
        log_level: str,
        max_restarts: int | None,
    ) -> None:
        super().__init__(name=camera.name, command=[], kind="publisher")
        self.camera = camera
        self.target_url = target_url
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.log_level = log_level
        self.max_restarts = max_restarts
        self.info: MediaInfo | None = None
        self.state = "waiting-for-source"
        self.source_failures = 0
        self.launches = 0
        self.failed_restarts = 0
        self._planned_restart = False
        self._source_thread: threading.Thread | None = None
        self._source_done = threading.Event()
        self._source_error: str | None = None

    def start(self) -> bool:
        if self.running or self._source_thread is not None or self.suspended or self.held:
            return True
        if shutil.which(self.ffprobe) is None:
            logger.error("%s: %s not found on PATH", self.name, self.ffprobe)
            self.gave_up = True
            self.state = "failed"
            return False
        self._source_done.clear()
        self._source_thread = threading.Thread(
            target=self._probe_source, name=f"vcam-source-{self.name}", daemon=True
        )
        self._source_thread.start()
        return True

    def _probe_source(self) -> None:
        try:
            self.info = probe(
                self.camera.source, ffprobe=self.ffprobe, timeout=self.camera.source_timeout
            )
            self._source_error = None if self.info.codec else "source has no video stream"
        except ProbeError as exc:
            self.info = None
            self._source_error = str(exc)
        finally:
            self._source_done.set()

    def tick(self, now: float) -> None:
        if self.held:
            if self.process is not None and not self.running:
                self.note_exit(self.process.poll(), planned=True)
            self.state = "stopped"
            return
        if self.running:
            self.state = "running"
            return
        if self.suspended or self.gave_up:
            return
        if self.process is not None:
            code = self.process.poll()
            logger.warning("%s: exited with code %s", self.name, code)
            self.note_exit(code)
            self.state = "failed"
            return
        if self._source_thread is not None:
            if not self._source_done.is_set():
                return
            self._source_thread.join()
            self._source_thread = None
            if self._source_error is not None:
                self.state = "waiting-for-source"
                self.source_failures += 1
                delay = min(BACKOFF_BASE * 2 ** min(self.source_failures - 1, 5), BACKOFF_MAX)
                self.retry_at = now + delay
                logger.warning(
                    "%s: waiting-for-source (%s); retry in %gs",
                    self.name,
                    self._source_error,
                    delay,
                )
                return
            outage = self.source_failures > 0
            self.source_failures = 0
            planned = self._planned_restart
            self._planned_restart = False
            if self.launches:
                if (
                    not outage
                    and not planned
                    and self.max_restarts is not None
                    and self.failed_restarts >= self.max_restarts
                ):
                    self.gave_up = True
                    self.state = "failed"
                    logger.error("%s: giving up (--max-restarts)", self.name)
                    return
                if not planned:
                    self.restarts += 1
                if not outage and not planned:
                    self.failed_restarts += 1
            self.command = build_publish_command(
                self.camera,
                self.target_url,
                info=self.info,
                ffmpeg=self.ffmpeg,
                log_level=self.log_level,
            )
            self.launches += 1
            self.state = "starting" if super().start() else "failed"
            return
        if now >= self.retry_at:
            self.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self.suspended or self.held:
            self._planned_restart = True
        if self._source_thread is not None:
            self._source_thread.join(timeout=self.camera.source_timeout + 1)
            self._source_thread = None
        super().stop(timeout)


@dataclass
class CameraRuntime:
    camera: CameraSpec
    instance: ServerInstance
    mode: StreamMode
    info: MediaInfo | None
    read_url: str
    """Credential-free URL, safe for logs and the health file."""
    read_url_with_credentials: str
    """URL a reader can use directly; carries credentials when auth is enabled."""
    process: ManagedProcess | SyncMember
    scheduler: SimulationScheduler | None = None
    """Drives the dropout cycle of a flaky camera, None otherwise."""
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class ReplayRuntime:
    replay: ReplaySpec
    read_url: str
    """Credential-free URL, safe for logs and the health file."""
    read_url_with_credentials: str
    process: ManagedProcess


def build_replay_command(
    replay: ReplaySpec,
    stack: CameraStack,
    *,
    host: str,
    verbose: bool = False,
) -> list[str]:
    """The `vcam replay` invocation the supervisor spawns for one capture."""
    command = [
        *vcam_command(),
        "replay",
        str(replay.source),
        "--path",
        replay.name,
        "--host",
        host,
        "--port",
        str(replay.port),
        "--speed",
        str(replay.speed),
    ]
    command.append("--loop" if replay.loop else "--no-loop")
    if not replay.rewrite_on_loop:
        command.append("--no-rewrite-on-loop")
    if replay.sdp is not None:
        command += ["--sdp", str(replay.sdp)]
    if stack.server.auth is not None:
        # The password goes through the environment instead: see
        # build_replay_env.
        command += ["--username", stack.server.auth.username]
    if verbose:
        command.append("--verbose")
    return command


def build_replay_env(stack: CameraStack) -> dict[str, str]:
    """Extra environment for a spawned `vcam replay`.

    The reader password is passed here rather than on the command line because
    argv is readable by every user on the box via /proc/<pid>/cmdline — the
    same reason the camera credentials go into a MediaMTX config file.
    """
    if stack.server.auth is None:
        return {}
    return {REPLAY_PASSWORD_ENV: stack.server.auth.password}


class SimulationScheduler:
    """Takes a `flaky` camera's publisher down and back up on a cycle.

    The publisher is flagged ``suspended`` for the duration, which keeps the
    monitor's crash-restart logic out of the scheduled cycle: a planned stop
    counts neither against the restart backoff nor the restart budget.

    Only ``flaky`` needs this. The other modes, ``stutter`` included, are
    expressed inside a single ffmpeg filter graph — swapping publishers mid-run
    tears the path's stream down and leaves attached readers stalled for good.
    """

    def __init__(self, runtime: CameraRuntime, spec: SimulationSpec, now: float) -> None:
        self.runtime = runtime
        if isinstance(runtime.process, SyncMember):
            raise SupervisorError("simulations are not supported on synchronized publishers")
        self.process = runtime.process
        self.spec = spec
        self.state = "up"  # "up" | "event"
        self._next_event = now + spec.interval

    @property
    def state_label(self) -> str:
        return "up" if self.state == "up" else "down"

    def tick(self, now: float) -> None:
        if now < self._next_event:
            return
        if self.state == "up":
            self._begin(now)
        else:
            self._end(now)

    def _begin(self, now: float) -> None:
        logger.info(
            "%s: [simulation] dropping the stream for %gs",
            self.runtime.camera.name,
            self.spec.duration,
        )
        self.process.suspended = True
        self.process.stop()
        self.state = "event"
        self._next_event = now + self.spec.duration

    def _end(self, now: float) -> None:
        logger.info("%s: [simulation] stream restored", self.runtime.camera.name)
        self.process.suspended = False
        if not getattr(self.process, "held", False):
            self.process.start()
        self.state = "up"
        self._next_event = now + self.spec.interval


@dataclass
class EngineCommand:
    action: str  # "start" | "stop" | "restart"
    future: Future = field(default_factory=Future)


@dataclass
class CameraCommand:
    key: CameraKey
    action: str  # "start" | "stop" | "restart"
    future: Future = field(default_factory=Future)


@dataclass
class ApplyCommand:
    """Make ``stack`` the running stack.

    Granular unless ``full``: a full apply restarts the whole engine and is
    the only way to apply a disruptive change.
    """

    stack: CameraStack
    full: bool = False
    before_apply: Callable[[], None] | None = None
    renamed: tuple[CameraKey, CameraKey] | None = None
    admission: MediaAdmission | None = None
    future: Future = field(default_factory=Future)


ControlCommand = EngineCommand | CameraCommand | ApplyCommand


class ApplyError(SupervisorError):
    """Some changes of a granular apply failed; the others took effect."""

    def __init__(self, failures: dict[str, str]) -> None:
        self.failures = failures
        super().__init__("; ".join(f"{name}: {error}" for name, error in failures.items()))


class Supervisor:
    """Owns the whole lifecycle: servers, publishers, health and shutdown.

    The thread running :meth:`run` is the only one that mutates runtime state.
    Other threads (the control API) interact through :meth:`submit`, whose
    commands the monitor loop executes, and through the ``on_tick`` hook,
    which also runs on that thread.
    """

    def __init__(
        self,
        stack: CameraStack,
        mediamtx_binary: Path | None,
        *,
        work_dir: Path,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        ffmpeg_log_level: str = "warning",
        health_file: Path | None = None,
        verify: bool = True,
        max_restarts: int | None = None,
        on_ready: Callable[[list[CameraRuntime]], None] | None = None,
        allow_empty: bool = False,
        on_tick: Callable[[Supervisor], None] | None = None,
    ) -> None:
        self.stack = stack
        self.mediamtx_binary = mediamtx_binary
        self.work_dir = work_dir
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.ffmpeg_log_level = ffmpeg_log_level
        self.health_file = health_file
        self.verify = verify
        self.max_restarts = max_restarts
        self.on_ready = on_ready
        self.allow_empty = allow_empty
        self.held_keys: set[CameraKey] = set()
        """Cameras stopped through the control API; survives engine stop/start."""
        """Controlled mode: an empty stack is valid, cameras come later."""
        self.on_tick = on_tick

        self.engine_state = "stopped"
        self.commands: queue.Queue[ControlCommand] = queue.Queue(maxsize=COMMAND_QUEUE_SIZE)
        self._admission_lock = threading.Lock()
        self._wake = threading.Event()
        self.instances: list[ServerInstance] = []
        self.servers: list[ManagedProcess] = []
        self.runtimes: list[CameraRuntime] = []
        self.replays: list[ReplayRuntime] = []
        self.sync_groups: list[SyncGroup] = []
        self._stop = threading.Event()

    @property
    def stopping(self) -> bool:
        """True once the daemon is exiting (not merely the engine stopped)."""
        return self._stop.is_set()

    # -- lifecycle -----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            logger.info("received signal %s, shutting down", signal.Signals(signum).name)
            self._stop.set()
            self._wake.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            # Not on the main thread (e.g. under a test runner); skip.
            with contextlib.suppress(ValueError):
                signal.signal(sig, handler)

    def prepare(self, admission: MediaAdmission | None = None) -> None:
        """Validate sources, plan instances and render server configs."""
        # Library callers and CLI overrides can mutate models after validation.
        self.stack = CameraStack.model_validate(self.stack.model_dump())
        self.instances, self.servers, self.runtimes = [], [], []
        self.replays, self.sync_groups = [], []
        cameras = self.stack.enabled_cameras
        replays = self.stack.enabled_replays
        if not cameras and not replays and not self.allow_empty:
            raise SupervisorError("no enabled cameras or replays to serve")

        missing = [
            camera
            for camera in cameras
            if isinstance(camera.source, Path) and not camera.source.is_file()
        ]
        missing_captures = [replay for replay in replays if not replay.source.is_file()]
        if missing or missing_captures:
            unresolved: list[CameraSpec | ReplaySpec] = [*missing, *missing_captures]
            details = "\n".join(f"  - {item.name}: {item.source}" for item in unresolved)
            raise SupervisorError(f"source file(s) not found:\n{details}")

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.instances = plan_instances(self.stack)
        if self.instances and self.mediamtx_binary is None:
            raise SupervisorError("a MediaMTX binary is required to serve cameras or ingest")

        for instance in self.instances:
            self.servers.append(self._server_process(instance))

        sync_sources = {
            camera.name: (
                admission.synchronized[Path(camera.source)]
                if admission and Path(camera.source) in admission.synchronized
                else admit_source(Path(camera.source))
            )
            for camera in cameras
            if camera.sync_group is not None
        }
        grouped: dict[str, list[CameraSpec]] = {}
        for camera in cameras:
            if camera.sync_group is not None:
                grouped.setdefault(camera.sync_group, []).append(camera)
        sync_members = {}
        for name, members in grouped.items():
            group = SyncGroup(
                name,
                [
                    (camera.name, sync_sources[camera.name], self.stack.publish_url(camera))
                    for camera in members
                ],
                self.max_restarts,
            )
            self.sync_groups.append(group)
            sync_members.update({member.name: member for member in group.members})

        for instance in self.instances:
            for camera in instance.cameras:
                self.runtimes.append(self._build_runtime(camera, instance, sync_members, admission))

        verbose = logger.isEnabledFor(logging.DEBUG)
        for replay in replays:
            self.replays.append(
                ReplayRuntime(
                    replay=replay,
                    read_url=self.stack.replay_url(replay, with_credentials=False),
                    read_url_with_credentials=self.stack.replay_url(replay),
                    process=ManagedProcess(
                        name=replay.name,
                        kind="replay",
                        command=build_replay_command(
                            replay,
                            self.stack,
                            host=self.stack.server.host,
                            verbose=verbose,
                        ),
                        env=build_replay_env(self.stack) or None,
                    ),
                )
            )

    def _server_process(self, instance: ServerInstance) -> ManagedProcess:
        def write_config() -> None:
            # MediaMTX watches its config file, so it is only (re)generated
            # while the server is down; a running server learns of granular
            # path changes through its API instead.
            write_server_config(instance, self.stack.server, self.work_dir, self.stack)

        write_config()
        assert instance.config_path is not None
        return ManagedProcess(
            name=instance.label,
            kind="server",
            command=[str(self.mediamtx_binary), str(instance.config_path)],
            before_start=write_config,
        )

    def _build_runtime(
        self,
        camera: CameraSpec,
        instance: ServerInstance,
        sync_members: dict[str, SyncMember],
        admission: MediaAdmission | None = None,
    ) -> CameraRuntime:
        info = None
        if not camera.is_live:
            info = admission.media.get(Path(camera.source)) if admission else None
            if info is None:
                info = try_probe(camera.source, ffprobe=self.ffprobe)
        if camera.sync_group is not None:
            mode = StreamMode.COPY
            publisher: ManagedProcess | SyncMember = sync_members[camera.name]
        else:
            mode = effective_mode(camera, info)
            publisher = (
                LivePublisher(
                    camera,
                    self.stack.publish_url(camera),
                    ffmpeg=self.ffmpeg,
                    ffprobe=self.ffprobe,
                    log_level=self.ffmpeg_log_level,
                    max_restarts=self.max_restarts,
                )
                if camera.is_live
                else ManagedProcess(
                    name=camera.name,
                    kind="publisher",
                    command=build_publish_command(
                        camera,
                        self.stack.publish_url(camera),
                        info=info,
                        ffmpeg=self.ffmpeg,
                        log_level=self.ffmpeg_log_level,
                    ),
                )
            )
        runtime = CameraRuntime(
            camera=camera,
            instance=instance,
            mode=mode,
            info=info,
            read_url=self.stack.read_url(camera, with_credentials=False),
            read_url_with_credentials=self.stack.read_url(camera),
            process=publisher,
        )
        if camera.simulation.mode is SimulationMode.FLAKY:
            runtime.scheduler = SimulationScheduler(runtime, camera.simulation, time.monotonic())
        return runtime

    def run(self) -> int:
        """Start everything and block until interrupted. Returns an exit code."""
        self._install_signal_handlers()
        try:
            self.prepare()
            return self._serve()
        finally:
            self.shutdown()

    def _serve(self) -> int:
        if not self._start_engine():
            return 0
        return self._monitor()

    def _start_engine(self, *, strict: bool = False) -> bool:
        """Start the prepared processes; False if a stop request cut it short."""
        self.engine_state = "starting"
        for server in self.servers:
            if not server.start():
                raise SupervisorError(f"could not start {server.name}")

        for instance in self.instances:
            host = (
                self.stack.ingest.host
                if instance.ingest and self.stack.ingest is not None
                else "127.0.0.1"
            )
            if host in {"0.0.0.0", "::", ""}:
                host = "127.0.0.1" if host != "::" else "::1"
            if not _wait_for_port(host, instance.rtsp_port, timeout=15.0, stop=self._stop):
                if self._stop.is_set():
                    return False
                # The RTSP port is the one being waited on, but MediaMTX exits
                # if *any* of its listeners collide, so naming only the RTSP
                # port sends people looking at the wrong thing.
                raise SupervisorError(
                    f"MediaMTX did not open RTSP port {instance.rtsp_port} in time "
                    f"(it also needs API port {instance.api_port} and UDP "
                    f"{instance.rtp_port}-{instance.rtp_port + UDP_BLOCK_SIZE - 1}; "
                    "check the MediaMTX log above for 'address already in use')"
                )

        if self._stop.is_set():
            return False
        for runtime in self.runtimes:
            if isinstance(runtime.process, ManagedProcess):
                if camera_key(self.stack, runtime.camera) in self.held_keys:
                    runtime.process.held = True
                    continue
                if not runtime.process.start() and strict:
                    raise SupervisorError(f"could not start camera {runtime.camera.name}")
        for group in self.sync_groups:
            group.start(service=self._tick_sync_groups)

        for replay in self.replays:
            if not replay.process.start() and strict:
                raise SupervisorError(f"could not start replay {replay.replay.name}")

        if self.verify:
            self._verify_streams()
            self._verify_replays()

        if self._stop.is_set():
            return False
        self.engine_state = "running"
        if self.on_ready is not None:
            self.on_ready(self.runtimes)
        return True

    def _monitor(self) -> int:
        health_interval = 5.0
        last_health = 0.0
        last_tick = 0.0

        while not self._stop.is_set():
            handled = self._drain_commands(limit=1)
            if self._stop.is_set():
                break
            now = time.monotonic()
            self._tick_sync_groups()

            for process in self._all_processes():
                if isinstance(process, LivePublisher):
                    process.tick(now)
                    continue
                if process.held:
                    if process.process is not None and not process.running:
                        process.note_exit(process.process.poll(), planned=True)
                    continue
                if process.running:
                    continue
                if process.process is not None:
                    code = process.process.poll()
                    if process.suspended:
                        logger.info("%s: stopped (simulation)", process.name)
                        process.note_exit(code, planned=True)
                    else:
                        level = logger.info if self._stop.is_set() else logger.warning
                        level("%s: exited with code %s", process.name, code)
                        process.note_exit(code)
                    continue
                if process.suspended:
                    # The simulation scheduler owns this stop/start cycle.
                    continue
                if now < process.retry_at:
                    continue
                if (
                    self.max_restarts is not None
                    and process.restarts - process.restart_budget_origin >= self.max_restarts
                ):
                    if not process.gave_up:
                        process.gave_up = True
                        logger.error(
                            "%s: giving up after %s restarts (--max-restarts)",
                            process.name,
                            process.restarts,
                        )
                    continue
                process.restarts += 1
                logger.info("%s: restart #%s", process.name, process.restarts)
                process.start()

            for runtime in self.runtimes:
                if isinstance(runtime.process, LivePublisher):
                    runtime.info = runtime.process.info
                    runtime.mode = effective_mode(runtime.camera, runtime.info)
                if runtime.scheduler is not None:
                    runtime.scheduler.tick(now)

            if self.health_file is not None and now - last_health >= health_interval:
                self._write_health()
                last_health = now

            if handled:
                last_tick = now
            elif self.on_tick is not None and now - last_tick >= 1.0:
                self.on_tick(self)
                last_tick = now

            if not self.commands.empty():
                continue
            self._wake.wait(0.1 if self.sync_groups else 1.0)
            self._wake.clear()

        self.shutdown()
        return 0

    def _all_processes(self) -> list[ManagedProcess]:
        return [
            *self.servers,
            *(
                runtime.process
                for runtime in self.runtimes
                if isinstance(runtime.process, ManagedProcess)
            ),
            *(replay.process for replay in self.replays),
        ]

    def shutdown(self) -> None:
        """Stop every owned child for good: the daemon is exiting."""
        with self._admission_lock:
            self._stop.set()
        self._cancel_pending()
        self._wake.set()
        self._stop_all()
        self.engine_state = "stopped"
        if self.health_file is not None:
            self._write_health()

    def _stop_all(self) -> None:
        actions = [
            *(replay.process.stop for replay in self.replays),
            *(
                runtime.process.stop
                for runtime in self.runtimes
                if isinstance(runtime.process, ManagedProcess)
            ),
            *(group.stop for group in self.sync_groups),
            *(server.stop for server in self.servers),
        ]
        errors = []
        for action in actions:
            try:
                action()
            except (OSError, SupervisorError) as exc:
                logger.error("resource cleanup failed: %s", exc)
                errors.append(str(exc))
        if errors:
            raise SupervisorError("shutdown failed: " + "; ".join(errors))

    # -- control commands (executed on the monitor thread) --------------------

    def submit(self, command: ControlCommand) -> Future:
        """Queue a command for the monitor loop; raises ``queue.Full`` when busy."""
        with self._admission_lock:
            if self._stop.is_set():
                raise SupervisorError("vcam is shutting down")
            self.commands.put_nowait(command)
        self._wake.set()
        return command.future

    def _drain_commands(self, limit: int = COMMAND_QUEUE_SIZE) -> bool:
        handled = False
        for _ in range(limit):
            if self._stop.is_set():
                break
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            handled = True
            if not command.future.set_running_or_notify_cancel():
                continue
            try:
                try:
                    result = self._execute(command)
                finally:
                    # A client polling a terminal operation must already see the
                    # resulting topology, including a partially applied failure.
                    if self.on_tick is not None:
                        self.on_tick(self)
            except Exception as exc:  # reported through the future
                command.future.set_exception(exc)
            else:
                command.future.set_result(result)
        if self._stop.is_set():
            self._cancel_pending()
        return handled

    def _cancel_pending(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            command.future.cancel()

    def _execute(self, command: ControlCommand) -> dict[str, Any]:
        if isinstance(command, EngineCommand):
            return self._engine_action(command.action)
        if isinstance(command, CameraCommand):
            return self._camera_action(command.key, command.action)
        if command.before_apply is not None:
            command.before_apply()
        if command.renamed and command.renamed[0] in self.held_keys:
            self.held_keys.add(command.renamed[1])
        return self._apply(command.stack, full=command.full, admission=command.admission)

    def _engine_stop(self) -> None:
        """Stop serving but keep the loop (and the control API) alive."""
        self.engine_state = "stopping"
        try:
            self._stop_all()
        finally:
            self.instances, self.servers, self.runtimes = [], [], []
            self.replays, self.sync_groups = [], []
            self.engine_state = "stopped"

    def _engine_start(self, admission: MediaAdmission | None = None) -> None:
        try:
            self.prepare(admission)
            if not self._start_engine(strict=True):
                raise SupervisorError("start interrupted by shutdown")
        except Exception:
            self._engine_stop()
            self.engine_state = "failed"
            raise

    def _engine_action(self, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"unknown engine action {action!r}")
        if action in {"stop", "restart"} and self.engine_state != "stopped":
            self._engine_stop()
        if action in {"start", "restart"} and self.engine_state != "running":
            self._engine_start()
        return {"engine": self.engine_state}

    def runtime_for(self, key: CameraKey) -> CameraRuntime | None:
        return next(
            (r for r in self.runtimes if camera_key(self.stack, r.camera) == key),
            None,
        )

    def _camera_action(self, key: CameraKey, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"unknown camera action {action!r}")
        runtime = self.runtime_for(key)
        if runtime is None and action != "stop" and self.engine_state == "running":
            camera = enabled_by_key(self.stack).get(key)
            if camera is not None and camera.sync_group is None:
                # Declared but not served (an earlier start failed): retry it.
                self.held_keys.discard(key)
                new_instance = self._instance_for(key[0]) is None
                if new_instance:
                    planned = plan_instances(self.stack, existing=self.instances)
                    self._start_instance(next(i for i in planned if i.rtsp_port == key[0]))
                self._add_camera(camera, key, declare_path=not new_instance)
                runtime = self.runtime_for(key)
        if runtime is None:
            raise LookupError(f"camera {key[1]!r} on port {key[0]} is not being served")
        process = runtime.process
        if isinstance(process, SyncMember):
            raise SupervisorError(
                "synchronized cameras start and stop with their group; use an engine action"
            )
        if action in {"stop", "restart"}:
            process.held = True
            self.held_keys.add(key)
            process.stop()
        if action in {"start", "restart"}:
            process.held = False
            self.held_keys.discard(key)
            process.gave_up = False
            process.restart_budget_origin = process.restarts
            if action == "restart":
                process.manual_restarts += 1
            process.consecutive_failures = 0
            process.retry_at = 0.0
            if isinstance(process, LivePublisher):
                process.failed_restarts = 0
                process.state = "waiting-for-source"
            if not process.start():
                raise SupervisorError(f"camera {key[1]!r} on port {key[0]} failed to start")
        return {"camera": {"port": key[0], "name": key[1]}, "state": self._camera_state(runtime)}

    def _apply(
        self, stack: CameraStack, *, full: bool, admission: MediaAdmission | None = None
    ) -> dict[str, Any]:
        stack = CameraStack.model_validate(stack.model_dump())
        self.held_keys &= {camera_key(stack, c) for c in stack.enabled_cameras}
        if self.engine_state == "failed":
            self.stack = stack
            self._engine_start(admission)
            return {"mode": "recovered", "engine": self.engine_state}
        if self.engine_state != "running":
            # Nothing is serving: the next engine start picks the stack up.
            self.stack = stack
            return {"mode": "stored", "engine": self.engine_state}
        plan = plan_changes(self.stack, stack)
        # Retry what an earlier partial apply left out: declared cameras with
        # no runtime are (re)added even when the YAML itself did not change.
        desired = enabled_by_key(stack)
        missing = tuple(
            key
            for key in enabled_by_key(self.stack)
            if key in desired and key not in plan.added and self.runtime_for(key) is None
        )
        if missing:
            plan = dataclasses.replace(
                plan,
                added=(*plan.added, *missing),
                updated=tuple(key for key in plan.updated if key not in missing),
            )
        # A private API failure can also leave an old (held) publisher to remove
        # or replace. A second apply must reconcile reality, not just diff YAML.
        for runtime in self.runtimes:
            key = (runtime.instance.rtsp_port, runtime.camera.name)
            if key not in desired and key not in plan.removed:
                plan = dataclasses.replace(plan, removed=(*plan.removed, key))
            elif key in desired and runtime.camera != desired[key] and key not in plan.updated:
                plan = dataclasses.replace(plan, updated=(*plan.updated, key))
        if full:
            self._engine_stop()
            self.stack = stack
            self._engine_start(admission)
            return {"mode": "full", "plan": plan.to_dict(), "engine": self.engine_state}
        if plan.disruptive:
            raise SupervisorError(
                "change needs a full apply: " + "; ".join(plan.disruptive_reasons)
            )
        failures: dict[str, str] = {}
        current = {camera_key(stack, c): c for c in stack.enabled_cameras}
        for key in (*plan.removed, *plan.updated):
            try:
                self._remove_camera(key, keep_path=key in plan.updated)
            except (MediaMTXAPIError, OSError, SupervisorError) as exc:
                failures[f"{key[1]}@{key[0]}"] = str(exc)
        self.stack = stack
        new_ports = {key[0] for key in plan.added} - {i.rtsp_port for i in self.instances}
        for instance in plan_instances(stack, existing=self.instances):
            if instance.rtsp_port not in new_ports:
                continue
            try:
                self._start_instance(instance)
            except (OSError, SupervisorError) as exc:
                for camera in instance.cameras:
                    failures[f"{camera.name}@{instance.rtsp_port}"] = str(exc)
        for key in (*plan.updated, *plan.added):
            label = f"{key[1]}@{key[0]}"
            if label in failures:
                continue
            try:
                # Retained paths and paths of a freshly started server (written
                # into its config) already exist; only new paths on a running
                # server are declared through the API.
                declare = key not in plan.updated and key[0] not in new_ports
                self._add_camera(current[key], key, declare_path=declare, admission=admission)
            except (MediaMTXAPIError, OSError, SupervisorError) as exc:
                failures[label] = str(exc)
        if failures:
            raise ApplyError(failures)
        return {"mode": "granular", "plan": plan.to_dict(), "engine": self.engine_state}

    def _instance_for(self, port: int) -> ServerInstance | None:
        return next((i for i in self.instances if i.rtsp_port == port), None)

    def _start_instance(self, instance: ServerInstance) -> None:
        if self.mediamtx_binary is None:
            raise SupervisorError("a MediaMTX binary is required to serve cameras")
        server = self._server_process(instance)
        self.instances.append(instance)
        self.servers.append(server)
        if not server.start() or not _wait_for_port(
            "127.0.0.1", instance.rtsp_port, timeout=15.0, stop=self._stop
        ):
            self._stop_instance(instance)
            raise SupervisorError(f"MediaMTX did not open RTSP port {instance.rtsp_port}")
        instance.cameras = []  # cameras join one by one through _add_camera

    def _stop_instance(self, instance: ServerInstance) -> None:
        for server in [s for s in self.servers if s.name == instance.label]:
            server.stop()
            self.servers.remove(server)
        if instance in self.instances:
            self.instances.remove(instance)
        if instance.config_path is not None:
            instance.config_path.unlink(missing_ok=True)

    def _add_camera(
        self,
        camera: CameraSpec,
        key: CameraKey,
        *,
        declare_path: bool,
        admission: MediaAdmission | None = None,
    ) -> None:
        instance = self._instance_for(key[0])
        if instance is None:
            raise SupervisorError(f"no MediaMTX instance serves port {key[0]}")
        runtime = self._build_runtime(camera, instance, {}, admission)
        if declare_path:
            MediaMTXClient(instance.api_url).add_path(camera.path_suffix())
        process = runtime.process
        if isinstance(process, ManagedProcess):
            if key in self.held_keys:
                process.held = True
            elif not process.start():
                process.stop()
                if (
                    not instance.cameras
                    and not instance.ingest
                    and not any(
                        c != camera and self.stack.effective_port(c) == key[0]
                        for c in self.stack.enabled_cameras
                    )
                ):
                    self._stop_instance(instance)
                else:
                    MediaMTXClient(instance.api_url).delete_path(camera.path_suffix())
                raise SupervisorError(f"camera {key[1]!r} on port {key[0]} failed to start")
        instance.cameras.append(camera)
        self.runtimes.append(runtime)

    def _remove_camera(self, key: CameraKey, *, keep_path: bool) -> None:
        runtime = self.runtime_for(key)
        if runtime is None:
            return
        if isinstance(runtime.process, ManagedProcess):
            runtime.process.held = True
            runtime.process.stop()
        instance = runtime.instance
        remaining = [c for c in instance.cameras if c.name != key[1]]
        if not keep_path:
            if not remaining and not instance.ingest:
                self._stop_instance(instance)
            else:
                MediaMTXClient(instance.api_url).delete_path(runtime.camera.path_suffix())
        instance.cameras = remaining
        self.runtimes.remove(runtime)

    # -- verification & health ----------------------------------------------

    def _tick_sync_groups(self) -> None:
        for group in self.sync_groups:
            if group.epoch_ns:
                group.tick(time.monotonic())

    def _verify_streams(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        pending = {
            runtime.camera.name: runtime
            for runtime in self.runtimes
            if not getattr(runtime.process, "held", False)
        }

        while pending and time.monotonic() < deadline and not self._stop.is_set():
            self._tick_sync_groups()
            for runtime in self.runtimes:
                if isinstance(runtime.process, LivePublisher):
                    runtime.process.tick(time.monotonic())
            for instance in self.instances:
                ready = _ready_paths(instance.api_url)
                for name in list(pending):
                    if pending[name].instance is instance and name in ready:
                        logger.info("%s: ready at %s", name, pending[name].read_url)
                        del pending[name]
            if pending:
                self._stop.wait(0.1 if self.sync_groups else 0.5)

        if self._stop.is_set():
            return
        for name in pending:
            logger.warning("%s: not publishing yet (check the ffmpeg log above)", name)

    def _verify_replays(self, timeout: float = 15.0) -> None:
        for replay in self.replays:
            if _wait_for_port(
                "127.0.0.1",
                replay.replay.port,
                timeout=timeout,
                service=self._tick_sync_groups,
                stop=self._stop,
            ):
                logger.info("%s: ready at %s", replay.replay.name, replay.read_url)
            elif self._stop.is_set():
                return
            else:
                logger.warning(
                    "%s: replay did not open RTSP port %s", replay.replay.name, replay.replay.port
                )

    def _camera_state(self, runtime: CameraRuntime) -> str:
        process = runtime.process
        if self._stop.is_set():
            return "stopped"
        if isinstance(process, ManagedProcess) and process.held:
            return "stopped"
        if isinstance(process, ManagedProcess) and process.suspended:
            return "suspended"
        if isinstance(process, LivePublisher):
            if process.running:
                return (
                    "running"
                    if runtime.camera.name in _ready_paths(runtime.instance.api_url)
                    else "starting"
                )
            return process.state
        return "running" if process.running else "failed"

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "servers": [
                {
                    "name": server.name,
                    "running": server.running,
                    "pid": server.pid,
                    "restarts": server.restarts,
                    "last_exit_code": server.last_exit_code,
                }
                for server in self.servers
            ],
            "cameras": [
                {
                    "name": runtime.camera.name,
                    "url": runtime.read_url,
                    "mode": runtime.mode.value,
                    "source": display_source(runtime.camera.source),
                    "state": self._camera_state(runtime),
                    "running": runtime.process.running,
                    "pid": runtime.process.pid,
                    "restarts": runtime.process.restarts,
                    "last_exit_code": runtime.process.last_exit_code,
                    "simulation": runtime.camera.simulation.mode.value,
                    **(runtime.process.health() if isinstance(runtime.process, SyncMember) else {}),
                    **(
                        {"simulation_state": runtime.scheduler.state_label}
                        if runtime.scheduler is not None
                        else {}
                    ),
                }
                for runtime in self.runtimes
            ],
            "replays": [
                {
                    "name": replay.replay.name,
                    "url": replay.read_url,
                    "source": str(replay.replay.source),
                    "running": replay.process.running,
                    "pid": replay.process.pid,
                    "restarts": replay.process.restarts,
                    "last_exit_code": replay.process.last_exit_code,
                }
                for replay in self.replays
            ],
            **(
                {
                    "sync_groups": [
                        {
                            "name": group.name,
                            "epoch_ns": group.epoch_ns or None,
                            "rate": str(group.members[0].source.rate),
                            "frames": group.members[0].source.frames,
                            "lead_frames": group.lead,
                        }
                        for group in self.sync_groups
                    ]
                }
                if self.sync_groups
                else {}
            ),
        }

    def _write_health(self) -> None:
        if self.health_file is None:
            return
        try:
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.health_file.with_suffix(self.health_file.suffix + ".tmp")
            tmp.write_text(json.dumps(self.health_snapshot(), indent=2), encoding="utf-8")
            os.replace(tmp, self.health_file)
        except OSError as exc:
            logger.error("could not write health file: %s", exc)


def _wait_for_port(
    host: str,
    port: int,
    timeout: float = 10.0,
    service: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
) -> bool:
    """Wait until ``host:port`` accepts connections; give up early once ``stop`` is set."""
    stop = stop or threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stop.is_set():
        if service is not None:
            service()
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            stop.wait(0.1 if service is not None else 0.2)
    return False


def _ready_paths(api_url: str) -> set[str]:
    """Names of paths currently receiving data, via the MediaMTX HTTP API."""
    try:
        with urlopen(f"{api_url}/v3/paths/list", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return set()
    return {
        item.get("name", "")
        for item in payload.get("items", [])
        if item.get("ready") and item.get("name")
    }
