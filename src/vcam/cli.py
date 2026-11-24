"""Typer command line interface for the virtual RTSP camera tool."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import __version__, binaries, control_cli
from . import service as _service
from .cli_helpers import OverridesResolver, RunContext
from .config import (
    dump_stack,
    example_stack,
    find_default_config,
    format_validation_error,
    load_stack,
    parse_stack_text,
)
from .config_store import ConfigStore, camera_fields, camera_nodes
from .errors import (
    ConfigError,
    NTPError,
    PcapError,
    ProbeError,
    ServiceError,
    SupervisorError,
)
from .ffmpeg import build_publish_command, resolve_mode
from .mediamtx import needs_server, plan_instances, render_server_config_yaml
from .models import (
    CAMERA_NAME_RE,
    CREDENTIAL_CHARS,
    CREDENTIAL_RE,
    RTSP_PASSTHROUGH_CODECS,
    AuthSpec,
    CameraSpec,
    CameraStack,
    ServerSpec,
    SimulationMode,
    StreamMode,
    Transport,
    VideoCodec,
)
from .ntp import apply_offset, has_sys_time_cap, measure_offset, running_in_container
from .pcap import backend_version as pcap_backend_version
from .probe import probe as probe_source
from .probe import try_probe
from .sim_manifest import import_sim as import_sim_manifest
from .sources import display_source, is_live_source, parse_source
from .supervisor import REPLAY_PASSWORD_ENV, CameraRuntime, LivePublisher, Supervisor

console = Console()
error_console = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Serve video files or live sources as virtual RTSP cameras.\n\n"
        "Cameras share one RTSP port and are addressed by path "
        "(rtsp://host:8554/cam1), unless a camera overrides `port` in its config."
    ),
)


# ---------------------------------------------------------------------------
# Shared option groups
# ---------------------------------------------------------------------------

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="YAML config file (default: ./cameras.yaml, .yml, or vcam.yaml/.yml).",
    ),
]
HostOption = Annotated[
    str | None,
    typer.Option("--host", help="Bind address for the RTSP listener (default: 0.0.0.0)."),
]
OfflineOption = Annotated[
    bool,
    typer.Option(
        "--offline",
        help="Use only the YAML file, even if a `vcam run --control` instance owns it.",
    ),
]
DisplayHostOption = Annotated[
    str | None,
    typer.Option("--host", help="Hostname or IP to print in the URLs (default: from config)."),
]


def _fail(message: str, code: int = 1) -> typer.Exit:
    error_console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"vcam {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _video_overrides(
    resolution: str | None,
    fps: float | None,
    bitrate: str | None,
    codec: VideoCodec | None,
    encoder: str | None,
    gop: int | None,
    preset: str | None,
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for key, value in (
        ("resolution", resolution),
        ("fps", fps),
        ("bitrate", bitrate),
        ("codec", codec),
        ("encoder", encoder),
        ("gop", gop),
        ("preset", preset),
    ):
        if value is not None:
            overrides[key] = value
    return overrides


def _simulation_overrides(
    simulation: SimulationMode | None,
    noise_level: int | None,
    interval: float | None,
    duration: float | None,
    filters: str | None,
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for key, value in (
        ("mode", simulation),
        ("noise_level", noise_level),
        ("interval", interval),
        ("duration", duration),
        ("filters", filters),
    ):
        if value is not None:
            overrides[key] = value
    return overrides


def _source_name(source: Path | str) -> str:
    if is_live_source(source):
        return _slug(Path(urlsplit(str(source)).path).name)
    return _slug(Path(source).stem)


def _parse_camera_argument(value: str) -> tuple[str, Path | str]:
    """Parse ``NAME=SOURCE`` or a bare file/live URL into a camera tuple."""
    if "=" in value and not is_live_source(value):
        name, _, source = value.partition("=")
        name = name.strip()
        source = source.strip()
        if not name or not source:
            raise typer.BadParameter(f"expected NAME=PATH, got {value!r}")
        return name, parse_source(source)
    source_value = parse_source(value)
    return _source_name(source_value), source_value


def _slug(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_-." else "-" for char in value)
    return cleaned.strip("-.") or "cam"


def _load_stack_or_exit(config: Path | None) -> CameraStack:
    path = config or find_default_config()
    if path is None:
        raise _fail("no config file found. Pass --config, or create one with `vcam init`.")
    try:
        return load_stack(path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _apply_overrides(
    stack: CameraStack,
    *,
    host: str | None,
    port: int | None,
    api_port: int | None,
    log_level: str | None,
    username: str | None,
    password: str | None,
    ntp_server: str | None,
    mode: StreamMode | None,
    loop: bool | None,
    realtime: bool | None,
    start_offset: float | None,
    transport: Transport | None,
    audio: bool | None,
    video: dict[str, object],
    simulation: dict[str, object],
) -> None:
    """Apply CLI overrides in place; every override applies to all cameras."""
    resolver = OverridesResolver()
    try:
        resolver.apply_overrides(
            stack,
            host=host,
            port=port,
            api_port=api_port,
            log_level=log_level,
            username=username,
            password=password,
            ntp_server=ntp_server,
            mode=mode,
            loop=loop,
            realtime=realtime,
            start_offset=start_offset,
            transport=transport,
            audio=audio,
            video=video,
            simulation=simulation,
        )
        CameraStack.model_validate(stack.model_dump())
    except ValueError as exc:
        raise _fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    config: ConfigOption = None,
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Video file or rtsp:// / udp:// live URL."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Camera name for --source (default: file stem)."),
    ] = None,
    camera: Annotated[
        list[str] | None,
        typer.Option(
            "--camera",
            help="Repeatable NAME=SOURCE pair (file path or live URL).",
        ),
    ] = None,
    host: HostOption = None,
    port: Annotated[
        int | None, typer.Option("--port", "-p", help="Shared RTSP port (default: 8554).")
    ] = None,
    api_port: Annotated[
        int | None,
        typer.Option("--api-port", help="MediaMTX HTTP API port, loopback only (default: 9997)."),
    ] = None,
    mode: Annotated[
        StreamMode | None,
        typer.Option(
            "--mode",
            help="auto: copy H.264/HEVC files and clean live feeds; transcode otherwise.",
        ),
    ] = None,
    loop: Annotated[
        bool | None, typer.Option("--loop/--no-loop", help="Loop the file forever (default: on).")
    ] = None,
    realtime: Annotated[
        bool | None,
        typer.Option(
            "--realtime/--no-realtime",
            help="Pace the file at native frame rate (default: on).",
        ),
    ] = None,
    start_offset: Annotated[
        float | None,
        typer.Option("--start-offset", help="Seek N seconds into the file (de-syncs feeds)."),
    ] = None,
    transport: Annotated[
        Transport | None,
        typer.Option("--transport", help="RTSP transport for publishing (default: tcp)."),
    ] = None,
    resolution: Annotated[
        str | None, typer.Option("--resolution", help="Transcode target, e.g. 1280x720.")
    ] = None,
    fps: Annotated[float | None, typer.Option("--fps", help="Transcode target frame rate.")] = None,
    bitrate: Annotated[
        str | None, typer.Option("--bitrate", help="Transcode target bitrate, e.g. 2M.")
    ] = None,
    codec: Annotated[VideoCodec | None, typer.Option("--codec", help="Transcode codec.")] = None,
    encoder: Annotated[
        str | None,
        typer.Option(
            "--encoder", help="Explicit ffmpeg encoder, e.g. h264_nvenc (overrides --codec)."
        ),
    ] = None,
    gop: Annotated[
        int | None, typer.Option("--gop", help="Keyframe interval in frames when transcoding.")
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="x264/x265 preset when transcoding (default: veryfast)."),
    ] = None,
    audio: Annotated[
        bool | None, typer.Option("--audio/--no-audio", help="Publish audio (off by default).")
    ] = None,
    simulation: Annotated[
        SimulationMode | None,
        typer.Option(
            "--simulation",
            help="Simulate a camera fault: noise, degraded, frozen, blackout, flaky, stutter.",
        ),
    ] = None,
    noise_level: Annotated[
        int | None,
        typer.Option(
            "--simulation-noise-level",
            "--noise-level",
            min=1,
            max=100,
            help="Noise amplitude for --simulation noise (default 30).",
        ),
    ] = None,
    simulation_interval: Annotated[
        float | None,
        typer.Option(
            "--simulation-interval",
            min=0.1,
            help="Seconds between flaky/stutter events (default 30).",
        ),
    ] = None,
    simulation_duration: Annotated[
        float | None,
        typer.Option(
            "--simulation-duration",
            min=0.1,
            help="How long each flaky/stutter event lasts (default 5).",
        ),
    ] = None,
    simulation_filters: Annotated[
        str | None,
        typer.Option("--simulation-filters", help="Extra ffmpeg filters, e.g. 'gblur=sigma=2'."),
    ] = None,
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="Require this user for readers.")
    ] = None,
    password: Annotated[
        str | None, typer.Option("--password", "-P", help="Password for --username.")
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option(
            "--server-log-level",
            help="MediaMTX log level: error, warn, info, debug (default: warn).",
        ),
    ] = None,
    ffmpeg_log_level: Annotated[
        str, typer.Option("--ffmpeg-log-level", help="ffmpeg -loglevel value.")
    ] = "warning",
    health_file: Annotated[
        Path | None,
        typer.Option("--health-file", help="Write a JSON health snapshot here every 5s."),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Keep generated MediaMTX configs in this directory."),
    ] = None,
    mediamtx_binary: Annotated[
        Path | None, typer.Option("--mediamtx-binary", help="Use this MediaMTX binary.")
    ] = None,
    mediamtx_version: Annotated[
        str, typer.Option("--mediamtx-version", help="Release to download when not installed.")
    ] = binaries.DEFAULT_VERSION,
    download: Annotated[
        bool, typer.Option("--download/--no-download", help="Allow downloading MediaMTX.")
    ] = True,
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Wait until every path is publishing.")
    ] = True,
    max_restarts: Annotated[
        int | None, typer.Option("--max-restarts", help="Give up after N restarts per process.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan and exit without starting anything.")
    ] = False,
    ntp_server: Annotated[
        str | None,
        typer.Option(
            "--ntp-server",
            help=(
                "Adjust the visible system clock from this NTP server before starting "
                "(e.g. 192.0.2.123). Docker only: rejected on bare CLI / service "
                "deployments, and measured but not applied without cap_add: [SYS_TIME]. "
                "On native Linux this may change the host clock."
            ),
        ),
    ] = None,
    control: Annotated[
        bool | None,
        typer.Option(
            "--control/--no-control",
            help=(
                "Serve the authenticated control API (default: the config's "
                "control.enabled). Inline camera changes last only until this process exits."
            ),
        ),
    ] = None,
    control_host: Annotated[
        str | None,
        typer.Option("--control-host", help="Control API bind address (default: 127.0.0.1)."),
    ] = None,
    control_port: Annotated[
        int | None,
        typer.Option(
            "--control-port", min=0, max=65535, help="Control API port; 0 picks a free one."
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Start the RTSP server and publish every camera. Runs until interrupted.

    With no --config/--source/--camera, the first of cameras.yaml, cameras.yml,
    vcam.yaml or vcam.yml in the current directory is used. Per-camera options
    below override the config for *every* camera in the stack.
    """
    controlled, config_path, revision, stack = _resolve_run_stack(
        config=config, source=source, name=name, cameras=camera, control=control
    )
    overrides: dict[str, object] = {
        "host": host,
        "port": port,
        "api_port": api_port,
        "log_level": log_level,
        "username": username,
        "password": password,
        "ntp_server": ntp_server,
        "mode": mode,
        "loop": loop,
        "realtime": realtime,
        "start_offset": start_offset,
        "transport": transport,
        "audio": audio,
        "video": _video_overrides(resolution, fps, bitrate, codec, encoder, gop, preset),
        "simulation": _simulation_overrides(
            simulation,
            noise_level,
            simulation_interval,
            simulation_duration,
            simulation_filters,
        ),
    }

    def effective(desired: CameraStack) -> CameraStack:
        """The YAML stack as this run serves it: CLI overrides re-applied."""
        result = desired.model_copy(deep=True)
        OverridesResolver().apply_overrides(result, **overrides)  # type: ignore[arg-type]
        if controlled:
            result.control.enabled = True
            if control_host is not None:
                result.control.host = control_host
            if control_port is not None:
                result.control.port = control_port
        return CameraStack.model_validate(result.model_dump())

    declared = stack
    try:
        stack = effective(declared)
    except ValidationError as exc:
        raise _fail(f"invalid options:\n{format_validation_error(exc)}") from exc
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    if not controlled and not stack.enabled_cameras and not stack.enabled_replays:
        raise _fail("no enabled cameras or replays to serve")

    if dry_run:
        _print_dry_run(stack, ffmpeg_log_level)
        return

    _setup_logging(verbose)

    _run_ntp_sync(stack.server.ntp_server)

    # A replay-only stack serves its captures itself and starts no MediaMTX,
    # so it must not need (or download) one.
    binary: Path | None = None
    # A controlled run can gain cameras later, so it always needs MediaMTX.
    if controlled or needs_server(stack):
        try:
            binary = binaries.resolve_binary(
                mediamtx_binary,
                version=mediamtx_version,
                allow_download=download,
                on_event=lambda message: console.print(f"[dim]{message}[/]"),
            )
        except binaries.BinaryError as exc:
            raise _fail(str(exc)) from exc

    temp_dir: str | None = None
    if work_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="vcam-")
        resolved_work_dir = Path(temp_dir)
    else:
        resolved_work_dir = work_dir

    context = RunContext(
        stack=stack,
        mediamtx_binary=binary,
        work_dir=resolved_work_dir,
        ffmpeg_log_level=ffmpeg_log_level,
        health_file=health_file,
        verify=verify,
        max_restarts=max_restarts,
        verbose=verbose,
    )

    supervisor = Supervisor(
        context.stack,
        context.mediamtx_binary,
        work_dir=context.work_dir,
        ffmpeg_log_level=context.ffmpeg_log_level,
        health_file=context.health_file,
        verify=context.verify,
        max_restarts=context.max_restarts,
        on_ready=lambda runtimes: _print_ready(context.stack, runtimes),
        allow_empty=controlled,
    )

    control_plane = None
    try:
        if controlled:
            control_plane = _start_control_plane(
                supervisor, config_path, revision, stack, effective, declared
            )
        raise typer.Exit(supervisor.run())
    except SupervisorError as exc:
        supervisor.shutdown()
        raise _fail(str(exc)) from exc
    except KeyboardInterrupt:
        supervisor.shutdown()
    finally:
        if control_plane is not None:
            control_plane.close()
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _resolve_run_stack(
    *,
    config: Path | None,
    source: Path | str | None,
    name: str | None,
    cameras: list[str] | None,
    control: bool | None,
) -> tuple[bool, Path | None, str | None, CameraStack]:
    """Decide whether this run is controlled and load its declared stack.

    Returns ``(controlled, config_path, revision, declared_stack)``. Inline
    control has no YAML path; its revision tracks canonical declared JSON.
    """
    if source is not None or cameras:
        return (
            bool(control),
            None,
            None,
            _build_stack(config=config, source=source, name=name, cameras=cameras),
        )
    path = config or find_default_config()
    if path is None:
        raise _fail("no config file found. Pass --config, or create one with `vcam init`.")
    if control is False:
        return False, path, None, _load_stack_or_exit(path)
    try:
        document = ConfigStore(path).read()
        declared = parse_stack_text(document.text, path, allow_empty=True)
    except ConfigError as exc:
        if control:
            raise _fail(str(exc)) from exc
        # Keep the established error messages for uncontrolled runs.
        return False, path, None, _load_stack_or_exit(path)
    except ValidationError as exc:
        if control:
            raise _fail(f"invalid config {path}:\n{format_validation_error(exc)}") from exc
        return False, path, None, _load_stack_or_exit(path)
    if control or declared.control.enabled:
        return True, path, document.revision, declared
    return False, path, None, _load_stack_or_exit(path)


class _ControlPlane:
    def __init__(self, controller: Any, server: Any) -> None:
        self.controller = controller
        self.server = server

    def close(self) -> None:
        try:
            self.server.stop()
        finally:
            logging.getLogger("vcam").removeHandler(self.controller.logs)
            self.controller.withdraw_discovery()


def _start_control_plane(
    supervisor: Supervisor,
    config_path: Path | None,
    revision: str | None,
    stack: CameraStack,
    effective: Callable[[CameraStack], CameraStack],
    declared: CameraStack,
) -> _ControlPlane:
    try:
        from .control import Controller
        from .control_api import ControlServer
    except ImportError as exc:
        raise _fail(
            "the control API needs the optional 'control' extra "
            "(from a source checkout: uv sync --extra control)"
        ) from exc
    controller = Controller(
        supervisor,
        config_path,
        effective=effective,
        spec=stack.control,
        revision=revision,
        declared=declared,
    )
    try:
        server = ControlServer(controller)
    except OSError as exc:
        raise _fail(
            f"cannot listen for the control API on {stack.control.host}:"
            f"{stack.control.port}: {exc.strerror or exc}"
        ) from exc
    plane = _ControlPlane(controller, server)
    with contextlib.ExitStack() as startup:
        startup.callback(plane.close)
        try:
            controller.claim_ownership()
            logging.getLogger("vcam").addHandler(controller.logs)
            server.start()
            controller.publish_discovery(server.endpoint, ca_file=stack.control.tls_cert)
        except (OSError, RuntimeError) as exc:
            raise _fail(str(exc)) from exc
        console.print(
            f"[bold]control API[/] {server.endpoint}/api/v1  "
            f"[dim](token file: {controller.token_file})[/]"
        )
        supervisor.on_tick = controller.tick
        startup.pop_all()
        return plane


def _build_stack(
    *,
    config: Path | None,
    source: Path | str | None,
    name: str | None,
    cameras: list[str] | None,
) -> CameraStack:
    inline: list[CameraSpec] = []

    try:
        if source is not None:
            inline.append(CameraSpec(name=name or _source_name(source), source=source))
        for entry in cameras or []:
            camera_name, camera_source = _parse_camera_argument(entry)
            inline.append(CameraSpec(name=camera_name, source=camera_source))
    except ValueError as exc:
        raise _fail(f"invalid camera definition: {exc}") from exc

    if inline and config is not None:
        raise _fail("use either --config or --source/--camera, not both")

    if inline:
        try:
            return CameraStack(cameras=inline)
        except ValidationError as exc:
            raise _fail(f"invalid camera definition:\n{format_validation_error(exc)}") from exc

    return _load_stack_or_exit(config)


def _run_ntp_sync(ntp_server: str | None) -> None:
    """Apply the measured offset; callers must use an isolated or intended clock."""
    if ntp_server is None:
        return

    logger = logging.getLogger("vcam")

    if not running_in_container():
        raise _fail(
            f"--ntp-server ({ntp_server}) is only supported when running inside a Docker "
            "container.\nApplying it on a bare CLI or systemd service would skew the host "
            "system clock.\nRemove --ntp-server, or run vcam inside an isolated Docker VM "
            "with cap_add: [SYS_TIME]. Native Linux containers share the host kernel clock."
        )

    console.print(f"[dim]NTP: querying {ntp_server} …[/]")
    try:
        offset, rtt = measure_offset(ntp_server)
    except NTPError as exc:
        raise _fail(f"NTP query to {ntp_server} failed: {exc}") from exc

    sign = "+" if offset >= 0 else ""
    if not has_sys_time_cap():
        console.print(
            f"[yellow]NTP:[/] measured offset {sign}{offset * 1000:.3f} ms "
            f"(RTT {rtt * 1000:.1f} ms) vs {ntp_server} — "
            "[yellow]not applied[/] (CAP_SYS_TIME unavailable; "
            "add [bold]cap_add: [SYS_TIME][/] to docker-compose.yml)"
        )
        return

    try:
        apply_offset(offset)
    except OSError as exc:
        raise _fail(f"Could not apply NTP offset: {exc}") from exc

    action = "stepped" if abs(offset) > 0.128 else "slewed"
    logger.info(
        "NTP sync to %s: offset %s%+.3f ms applied via %s (RTT %.1f ms)",
        ntp_server,
        sign,
        offset * 1000,
        action,
        rtt * 1000,
    )
    console.print(
        f"[green]NTP:[/] {sign}{offset * 1000:.3f} ms {action} "
        f"(RTT {rtt * 1000:.1f} ms, server {ntp_server})"
    )


def _print_dry_run(stack: CameraStack, ffmpeg_log_level: str) -> None:
    instances = plan_instances(stack)
    for instance in instances:
        console.print(f"[bold]# mediamtx-{instance.rtsp_port}.yml[/]")
        console.print(render_server_config_yaml(instance, stack.server, stack).rstrip())
        console.print()

    console.print("[bold]# publishers[/]")
    for camera in stack.enabled_cameras:
        if camera.sync_group is not None:
            console.print(
                f"[dim]# {camera.name} -> {stack.read_url(camera)}[/]\n"
                f"synchronized H.264 copy publisher (group={camera.sync_group}); "
                "source admission and shared startup barrier occur on run"
            )
            continue
        info = (
            try_probe(camera.source)
            if isinstance(camera.source, Path) and camera.source.is_file()
            else None
        )
        command = build_publish_command(
            camera,
            stack.publish_url(camera),
            info=info,
            log_level=ffmpeg_log_level,
        )
        console.print(f"[dim]# {camera.name} -> {stack.read_url(camera)}[/]")
        console.print(
            " ".join(
                _quote(display_source(part) if part == str(camera.source) else part)
                for part in command
            )
        )
    console.print()
    _print_camera_table(stack)


def _quote(value: str) -> str:
    return f"'{value}'" if " " in value else value


def _print_ready(stack: CameraStack, runtimes: list[CameraRuntime]) -> None:
    if runtimes:
        table = Table(title="Virtual RTSP cameras", title_justify="left")
        table.add_column("camera", style="bold cyan")
        table.add_column("url")
        table.add_column("mode")
        table.add_column("state")
        table.add_column("sim")
        if any(camera.sync_group is not None for camera in stack.cameras):
            table.add_column("sync group")
        table.add_column("source", style="dim")
        for runtime in runtimes:
            row = [
                runtime.camera.name,
                runtime.read_url_with_credentials,
                runtime.mode.value,
                runtime.process.state
                if isinstance(runtime.process, LivePublisher)
                else ("running" if runtime.process.running else "failed"),
                _simulation_label(runtime.camera),
            ]
            if any(camera.sync_group is not None for camera in stack.cameras):
                row.append(runtime.camera.sync_group or "-")
            table.add_row(*row, display_source(runtime.camera.source))
        console.print(table)
    _print_replay_table(stack)
    if stack.server.auth is not None:
        console.print("[dim]readers must authenticate; credentials are embedded above[/]")
    console.print("[dim]press Ctrl-C to stop[/]")


def _print_replay_table(stack: CameraStack, host: str | None = None) -> None:
    if not stack.replays:
        return
    table = Table(title="Capture replays", title_justify="left")
    table.add_column("replay", style="bold magenta")
    table.add_column("url")
    table.add_column("loop")
    table.add_column("speed")
    table.add_column("capture", style="dim")
    for replay in stack.replays:
        table.add_row(
            replay.name if replay.enabled else f"[strike]{replay.name}[/]",
            stack.replay_url(replay, host),
            "yes" if replay.loop else "no",
            f"{replay.speed:g}x",
            str(replay.source),
        )
    console.print(table)


def _simulation_label(camera: CameraSpec) -> str:
    """Short description of a camera's simulation for the CLI tables."""
    sim = camera.simulation
    if sim.mode is SimulationMode.NORMAL:
        return "-" if not sim.filters else "filters"
    if sim.is_temporal:
        return f"{sim.mode.value} {sim.duration:g}s/{sim.interval:g}s"
    return sim.mode.value


def _print_camera_table(stack: CameraStack, host: str | None = None) -> None:
    table = Table(title="Cameras", title_justify="left")
    table.add_column("camera", style="bold cyan")
    table.add_column("url")
    table.add_column("mode")
    table.add_column("sim")
    if any(camera.sync_group is not None for camera in stack.cameras):
        table.add_column("sync group")
    table.add_column("loop")
    table.add_column("offset")
    table.add_column("source", style="dim")
    for camera in stack.cameras:
        row = [
            camera.name if camera.enabled else f"[strike]{camera.name}[/]",
            stack.read_url(camera, host),
            camera.mode.value,
            _simulation_label(camera),
        ]
        if any(item.sync_group is not None for item in stack.cameras):
            row.append(camera.sync_group or "-")
        table.add_row(
            *row,
            "-" if camera.is_live else ("yes" if camera.loop else "no"),
            f"{camera.start_offset:g}s",
            display_source(camera.source),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# config management
# ---------------------------------------------------------------------------


def _output_revision(path: Path, force: bool) -> str | None:
    if not path.exists():
        return None
    if not force:
        raise _fail(f"{path} already exists (use --force to overwrite)")
    try:
        return ConfigStore(path).disk_revision()
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _write_generated(stack: CameraStack, path: Path, expected: str | None) -> None:
    with _offline_write(path):
        ConfigStore(path).commit(dump_stack(stack), expected_revision=expected)


@app.command("import-sim")
def import_sim(
    manifest: Annotated[Path, typer.Argument(help="sim-streams.json (eais-sim-streams/1).")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write YAML here (default: stdout).")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Convert simulator streams into a camera stack, without probing or connecting."""
    expected = _output_revision(output, force) if output is not None else None
    try:
        stack = import_sim_manifest(manifest)
        if output is None:
            print(dump_stack(stack), end="")
        else:
            _write_generated(stack, output, expected)
            console.print(f"wrote [bold]{output}[/]")
    except (ConfigError, OSError) as exc:
        raise _fail(str(exc)) from exc


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Config file to create.")] = Path("cameras.yaml"),
    source: Annotated[
        list[Path] | None,
        typer.Option("--source", "-s", help="Repeatable video file to seed the config with."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter configuration file."""
    expected = _output_revision(path, force)
    stack = example_stack([Path(item).expanduser() for item in source] if source else None)
    _write_generated(stack, path, expected)
    console.print(f"wrote [bold]{path}[/]")


@app.command()
def generate(
    path: Annotated[Path, typer.Argument(help="Config file to create.")] = Path("cameras.yaml"),
    scan: Annotated[
        Path | None,
        typer.Option("--scan", "-d", help="Directory to scan for video files (default: cwd)."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Interactive wizard that generates a cameras.yaml from your video files.

    Scans the given directory (or the current directory) for video files,
    then asks you a few questions for each one and about the server.
    The result is written to PATH (default: cameras.yaml).
    """
    expected = _output_revision(path, force)

    scan_dir = (scan or Path.cwd()).resolve()
    if not scan_dir.is_dir():
        raise _fail(f"not a directory: {scan_dir}")

    video_extensions = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".webm", ".flv", ".wmv"}
    candidates = sorted(
        p for p in scan_dir.iterdir() if p.is_file() and p.suffix.lower() in video_extensions
    )

    console.print("\n[bold cyan]vcam generate[/] — interactive configuration wizard\n")

    if not candidates:
        console.print(f"[yellow]No video files found in {scan_dir}.[/]")
        console.print(
            "Add video files there or pass [bold]--scan <dir>[/] to choose another directory."
        )
        raise typer.Exit(1)

    console.print(f"Found [bold]{len(candidates)}[/] video file(s) in [dim]{scan_dir}[/]:\n")
    for i, p in enumerate(candidates, 1):
        console.print(f"  [dim]{i:2}.[/] {p.name}")
    console.print()

    # --- collect cameras ---
    cameras: list[CameraSpec] = []
    seen_names: set[str] = set()

    for source_path in candidates:
        include = typer.confirm(f"Include [bold]{source_path.name}[/]?", default=True)
        if not include:
            continue

        # Suggest a name derived from the file stem
        default_name = _slug(source_path.stem)
        while True:
            raw_name = typer.prompt("  Camera name", default=default_name)
            if not CAMERA_NAME_RE.match(raw_name):
                console.print(
                    "  [red]Invalid name[/] — use letters, digits, '_', '-', or '.'",
                )
                continue
            if raw_name in seen_names:
                console.print(f"  [red]Name '{raw_name}' is already used[/] — choose another.")
                continue
            break
        seen_names.add(raw_name)

        # Probe to suggest copy vs transcode
        info = try_probe(source_path)
        if info and info.codec and info.codec.lower() in RTSP_PASSTHROUGH_CODECS:
            codec_hint = f"[green]{info.codec}[/] (can copy)"
            default_mode_val = StreamMode.COPY
        elif info and info.codec:
            codec_hint = f"[yellow]{info.codec}[/] (needs transcode)"
            default_mode_val = StreamMode.TRANSCODE
        else:
            codec_hint = "[dim]unknown[/]"
            default_mode_val = StreamMode.AUTO

        if info:
            res = info.resolution or "?"
            fps_str = f"{info.fps:.2f}" if info.fps else "?"
            dur_str = f"{info.duration:.0f}s" if info.duration else "?"
            console.print(
                f"  [dim]Detected:[/] codec={codec_hint}  "
                f"resolution={res}  fps={fps_str}  duration={dur_str}"
            )

        raw_mode = typer.prompt(
            "  Stream mode",
            default=default_mode_val.value,
            show_choices=True,
            show_default=True,
        )
        try:
            chosen_mode = StreamMode(raw_mode)
        except ValueError:
            chosen_mode = StreamMode.AUTO

        offset = typer.prompt("  Start offset (seconds)", default="0", show_default=True)
        try:
            start_offset = float(offset)
        except ValueError:
            start_offset = 0.0

        cameras.append(
            CameraSpec(
                name=raw_name,
                source=source_path.resolve(),
                mode=chosen_mode,
                start_offset=start_offset,
            )
        )
        console.print()

    if not cameras:
        console.print("[yellow]No cameras selected — nothing to write.[/]")
        raise typer.Exit(0)

    # --- server settings ---
    console.print("[bold]Server settings[/] (press Enter to accept defaults)\n")

    raw_port = typer.prompt("  RTSP port", default="8554", show_default=True)
    try:
        rtsp_port = int(raw_port)
    except ValueError:
        rtsp_port = 8554

    add_auth = typer.confirm("  Require authentication (username/password)?", default=False)
    auth: AuthSpec | None = None
    if add_auth:
        while True:
            username = typer.prompt("    Username")
            if CREDENTIAL_RE.match(username):
                break
            console.print(f"    [red]Invalid characters.[/] Allowed: {CREDENTIAL_CHARS}")
        while True:
            password = typer.prompt("    Password", hide_input=True)
            if CREDENTIAL_RE.match(password):
                break
            console.print(f"    [red]Invalid characters.[/] Allowed: {CREDENTIAL_CHARS}")
        auth = AuthSpec(username=username, password=password)

    server = ServerSpec(rtsp_port=rtsp_port, auth=auth)
    stack = CameraStack(server=server, cameras=cameras)

    # --- summary & write ---
    console.print(f"\n[bold green]Summary[/] — {len(cameras)} camera(s) on port {rtsp_port}:\n")
    for cam in cameras:
        console.print(f"  [bold]{cam.name}[/]  ({cam.mode.value})  ← {Path(cam.source).name}")
    console.print()

    _write_generated(stack, path, expected)
    console.print(f"wrote [bold]{path}[/]\n")
    console.print(
        f"Run [bold]vcam run --config {path}[/] to start, "
        "or [bold]vcam list --config " + str(path) + "[/] to review."
    )


@app.command()
def add(
    source: Annotated[str, typer.Argument(help="Video file or live URL to add as a camera.")],
    config: ConfigOption = None,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Camera name (default: file stem).")
    ] = None,
    mode: Annotated[
        StreamMode,
        typer.Option(
            "--mode",
            help="auto: copy H.264/HEVC files and clean live feeds; transcode otherwise.",
        ),
    ] = StreamMode.AUTO,
    simulation: Annotated[
        SimulationMode,
        typer.Option(
            "--simulation",
            help="Simulate a camera fault: noise, degraded, frozen, blackout, flaky, stutter.",
        ),
    ] = SimulationMode.NORMAL,
    start_offset: Annotated[
        float,
        typer.Option("--start-offset", help="Seek N seconds into the file (de-syncs feeds)."),
    ] = 0.0,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Dedicated RTSP port for this camera (spawns its own server instance).",
        ),
    ] = None,
    resolution: Annotated[
        str | None, typer.Option("--resolution", help="Transcode target, e.g. 1280x720.")
    ] = None,
    fps: Annotated[float | None, typer.Option("--fps", help="Transcode target frame rate.")] = None,
    bitrate: Annotated[
        str | None, typer.Option("--bitrate", help="Transcode target bitrate, e.g. 2M.")
    ] = None,
    codec: Annotated[
        VideoCodec | None, typer.Option("--codec", help="Transcode codec (default: h264).")
    ] = None,
    encoder: Annotated[
        str | None,
        typer.Option(
            "--encoder", help="Explicit ffmpeg encoder, e.g. h264_nvenc (overrides --codec)."
        ),
    ] = None,
    gop: Annotated[
        int | None, typer.Option("--gop", help="Keyframe interval in frames when transcoding.")
    ] = None,
    offline: OfflineOption = False,
    endpoint: control_cli.EndpointOption = None,
    token_file: control_cli.TokenFileOption = None,
    ca_file: control_cli.CaFileOption = None,
    as_json: control_cli.JsonOption = False,
    wait: control_cli.WaitOption = True,
) -> None:
    """Add a camera to a controlled engine or, offline, to its YAML file.

    With an explicit endpoint, file sources are paths on the engine host.
    An endpoint failure never falls back to a local file.
    """
    remote = control_cli.target(config, endpoint, token_file, ca_file, offline=offline)
    path = None if remote is not None else config or find_default_config()
    if remote is None and path is None:
        raise _fail("no config file found. Create one with `vcam init`.")
    if remote is None and not wait:
        raise _fail("--no-wait requires a running controlled engine")
    overrides = _video_overrides(resolution, fps, bitrate, codec, encoder, gop, None)
    try:
        camera_name = name or _source_name(source)
        values = {
            "name": camera_name,
            "source": (
                control_cli.engine_source(remote, source)
                if remote is not None
                else control_cli.absolute_source(source)
            ),
            "mode": mode.value,
            "simulation": {"mode": simulation.value},
            "video": overrides,
            "start_offset": start_offset,
            "port": port,
        }
        # The daemon validates an online camera on the filesystem that owns its source.
        if remote is None:
            values = CameraSpec.model_validate(values).model_dump(
                mode="json", exclude_defaults=True
            )
    except ValidationError as exc:
        raise _fail(f"invalid camera definition:\n{format_validation_error(exc)}") from exc
    except ValueError as exc:
        raise _fail(f"invalid camera definition: {exc}") from exc

    if remote is not None:
        with control_cli._handled():
            revision = remote.get("cameras")["revision"]
            op = remote.write("POST", "cameras", values, revision=revision, wait=wait)
        if as_json:
            control_cli._emit(op)
        else:
            control_cli.print_added(camera_name, op)
        return

    assert path is not None
    with _offline_write(path):
        store = ConfigStore(path)
        doc = store.read()
        camera_nodes(doc.data).append(camera_fields(doc.data, values))
        text = store.preview(doc)
        parse_stack_text(text, path, allow_empty=True)
        written = store.commit(text, expected_revision=doc.revision)
    if as_json:
        control_cli._emit(
            {
                "name": camera_name,
                "path": str(path),
                "revision": written.revision,
                "config_persisted": True,
                "applied": False,
            }
        )
    else:
        console.print(f"added [bold]{camera_name}[/] to {path} (file only)")


@contextlib.contextmanager
def _offline_write(path: Path) -> Iterator[None]:
    """Refuse to edit a config that a controlled instance claims meanwhile."""
    from .control import config_ownership

    try:
        with config_ownership(path.resolve()):
            yield
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    except SupervisorError as exc:
        if isinstance(exc.__cause__, BlockingIOError):
            raise _fail(f"a running `vcam run --control` owns {path}; use its control API") from exc
        raise _fail(f"cannot verify config ownership: {exc}") from exc


def _stack_streams(
    stack: CameraStack, host: str | None, include_disabled: bool
) -> list[dict[str, Any]]:
    """RTSP endpoints in the control API's `streams` shape: cameras first, then replays."""
    cameras = stack.cameras if include_disabled else stack.enabled_cameras
    replays = stack.replays if include_disabled else stack.enabled_replays
    return [
        *(
            {
                "port": stack.effective_port(camera),
                "name": camera.name,
                "protocol": "rtsp",
                "kind": "camera",
                "url": stack.read_url(camera, host),
            }
            for camera in cameras
        ),
        *(
            {
                "port": replay.port,
                "name": replay.name,
                "protocol": "rtsp",
                "kind": "replay",
                "url": stack.replay_url(replay, host),
            }
            for replay in replays
        ),
    ]


def _stack_listing(stack: CameraStack, host: str | None) -> dict[str, Any]:
    """Declared cameras and replays in the control API's list shape (no runtime state offline)."""
    return {
        "cameras": [
            {
                "port": stack.effective_port(camera),
                "name": camera.name,
                "enabled": camera.enabled,
                "url": stack.read_url(camera, host),
                "source": display_source(camera.source),
                "live": camera.is_live,
                "sync_group": camera.sync_group,
                "simulation": camera.simulation.mode.value,
            }
            for camera in stack.cameras
        ],
        "replays": [
            {
                "port": replay.port,
                "name": replay.name,
                "enabled": replay.enabled,
                "url": stack.replay_url(replay, host),
            }
            for replay in stack.replays
        ],
    }


@app.command("list")
def list_cameras(
    config: ConfigOption = None,
    host: DisplayHostOption = None,
    offline: OfflineOption = False,
    endpoint: control_cli.EndpointOption = None,
    token_file: control_cli.TokenFileOption = None,
    ca_file: control_cli.CaFileOption = None,
    as_json: control_cli.JsonOption = False,
) -> None:
    """Show the cameras and replays declared in a configuration file.

    With a running controlled instance (discovered, or named with --endpoint),
    shows its live camera state instead.
    """
    remote = control_cli.target(config, endpoint, token_file, ca_file, offline=offline)
    if remote is not None:
        control_cli.list_online(remote, host, as_json)
        return
    stack = _load_stack_or_exit(config)
    if as_json:
        control_cli._emit(_stack_listing(stack, host))
        return
    _print_camera_table(stack, host)
    _print_replay_table(stack, host)


@app.command()
def urls(
    config: ConfigOption = None,
    host: DisplayHostOption = None,
    all_cameras: Annotated[bool, typer.Option("--all", help="Include disabled cameras.")] = False,
    offline: OfflineOption = False,
    endpoint: control_cli.EndpointOption = None,
    token_file: control_cli.TokenFileOption = None,
    ca_file: control_cli.CaFileOption = None,
    as_json: control_cli.JsonOption = False,
) -> None:
    """Print one RTSP URL per line (handy for scripts and pipelines).

    Offline URLs carry the configured credentials; a running engine's never do.
    """
    remote = control_cli.target(config, endpoint, token_file, ca_file, offline=offline)
    if remote is not None:
        control_cli.urls_online(remote, host, all_cameras, as_json)
        return
    streams = _stack_streams(_load_stack_or_exit(config), host, all_cameras)
    if as_json:
        control_cli._emit({"urls": [stream["url"] for stream in streams], "streams": streams})
        return
    for stream in streams:
        print(stream["url"])


@app.command()
def show(
    config: ConfigOption = None,
    offline: OfflineOption = False,
    endpoint: control_cli.EndpointOption = None,
    token_file: control_cli.TokenFileOption = None,
    ca_file: control_cli.CaFileOption = None,
    as_json: control_cli.JsonOption = False,
) -> None:
    """Print the fully resolved configuration as YAML.

    With a running controlled instance (discovered, or named with --endpoint),
    prints what it serves (secrets redacted). The offline file is shown as written.
    """
    remote = control_cli.target(config, endpoint, token_file, ca_file, offline=offline)
    if remote is not None:
        control_cli.show_online(remote, as_json)
        return
    stack = _load_stack_or_exit(config)
    if as_json:
        path = config or find_default_config()
        assert path is not None
        resolved = yaml.safe_load(dump_stack(stack))
        control_cli._emit(
            {
                "path": str(path.resolve()),
                "declared": resolved,
                "effective": resolved,
                "provenance": {"declared": "file", "overridden": []},
            }
        )
        return
    console.print(dump_stack(stack).rstrip())


@app.command("clock-status")
def clock_status(
    ntp_server: Annotated[
        str | None,
        typer.Option(
            "--ntp-server",
            help=(
                "NTP server to measure the clock offset against "
                "(e.g. 192.0.2.123). Read-only: never adjusts the clock."
            ),
        ),
    ] = None,
) -> None:
    """Show clock status and measure offset against an NTP server (read-only)."""
    import time as _time

    in_container = running_in_container()
    has_cap = has_sys_time_cap()

    console.print(f"System time  : {_time.strftime('%Y-%m-%dT%H:%M:%S', _time.gmtime())} UTC")
    console.print(f"In container : {'[green]yes[/]' if in_container else '[yellow]no[/]'}")
    console.print(f"CAP_SYS_TIME : {'[green]yes[/]' if has_cap else '[yellow]no[/]'}")

    if ntp_server is None:
        if not in_container:
            console.print(
                "\n[dim]Tip: pass --ntp-server <host> to measure offset. "
                "NTP sync is only supported inside Docker containers.[/]"
            )
        return

    console.print(f"NTP server   : {ntp_server}")
    try:
        offset, rtt = measure_offset(ntp_server)
    except NTPError as exc:
        console.print(f"[red]NTP error    : {exc}[/]")
        raise typer.Exit(1) from exc

    sign = "+" if offset >= 0 else ""
    console.print(f"Offset       : {sign}{offset * 1000:.3f} ms")
    console.print(f"RTT          : {rtt * 1000:.3f} ms")

    if not in_container:
        console.print(
            "\n[yellow]Note:[/] NTP sync ([bold]--ntp-server[/] on [bold]vcam run[/]) "
            "is only supported inside a Docker container to avoid touching the host clock."
        )
    elif not has_cap:
        console.print(
            "\n[yellow]Note:[/] CAP_SYS_TIME is not set — offset is measured but cannot "
            "be applied.\nAdd [bold]cap_add: [SYS_TIME][/] only in an isolated VM; "
            "native Linux containers share the host kernel clock."
        )


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


@app.command()
def replay(
    capture: Annotated[Path, typer.Argument(help="Capture file to replay (.pcap or .pcapng).")],
    path: Annotated[
        str | None,
        typer.Option("--path", help="RTSP path to serve on (default: the capture file stem)."),
    ] = None,
    host: HostOption = None,
    port: Annotated[
        int, typer.Option("--port", "-p", help="RTSP port for the replay listener.")
    ] = 8554,
    loop: Annotated[
        bool, typer.Option("--loop/--no-loop", help="Replay the capture forever (default: on).")
    ] = True,
    rewrite_on_loop: Annotated[
        bool,
        typer.Option(
            "--rewrite-on-loop/--no-rewrite-on-loop",
            help=(
                "Keep RTP sequence numbers and timestamps monotonic across loops. "
                "Disable to reproduce a raw rewind (decoders usually stall)."
            ),
        ),
    ] = True,
    speed: Annotated[
        float, typer.Option("--speed", help="Playback speed multiplier (1.0 = as captured).")
    ] = 1.0,
    sdp: Annotated[
        Path | None,
        typer.Option("--sdp", help="Session description to use when the capture has no DESCRIBE."),
    ] = None,
    username: Annotated[
        str | None, typer.Option("--username", help="Require basic auth with this username.")
    ] = None,
    password: Annotated[
        str | None, typer.Option("--password", help="Password for --username.")
    ] = None,
    list_tracks: Annotated[
        bool,
        typer.Option("--list-tracks", help="Describe the capture and exit without serving."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
) -> None:
    """Replay RTP captured from a real camera, byte for byte, over RTSP.

    The packets are sent exactly as they were captured — same payloads, same
    SSRC, same fragmentation, same inter-packet timing — so a fault that only
    reproduces at the packet level still reproduces here. Nothing is decoded or
    re-packetised on the way out.
    """
    _setup_logging(verbose)

    capture_path = Path(capture).expanduser()
    if not capture_path.is_file():
        raise _fail(f"capture file not found: {capture_path}")
    if sdp is not None and not Path(sdp).expanduser().is_file():
        raise _fail(f"SDP file not found: {sdp}")
    # The supervisor passes the password through the environment so it never
    # appears in argv, where any user on the box could read it.
    secret = password if password is not None else os.environ.get(REPLAY_PASSWORD_ENV)
    if (username is None) != (secret is None):
        raise _fail(f"--username and --password (or ${REPLAY_PASSWORD_ENV}) must be given together")
    password = secret

    from . import replay_source as _replay_source
    from .rtsp_replay import ReplayServer

    try:
        source = _replay_source.load(
            capture_path, sdp_override=Path(sdp).expanduser() if sdp else None
        )
    except _replay_source.ReplaySourceError as exc:
        raise _fail(str(exc)) from exc
    except PcapError as exc:
        raise _fail(str(exc)) from exc

    for warning in source.warnings:
        error_console.print(f"[yellow]warning:[/] {warning}")

    console.print(_replay_source.describe(source))
    if list_tracks:
        return

    try:
        server = ReplayServer(
            source,
            host=host or "0.0.0.0",
            port=port,
            path=path or capture_path.stem,
            username=username,
            password=password,
            loop=loop,
            speed=speed,
            rewrite_on_loop=rewrite_on_loop,
        )
    except OSError as exc:
        raise _fail(f"could not bind RTSP port {port}: {exc}") from exc
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    console.print(f"serving [bold]{server.rtsp_url()}[/]")
    console.print("[dim]press Ctrl-C to stop[/]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("stopping")
    finally:
        server.shutdown()


@app.command()
def probe(
    source: Annotated[str, typer.Argument(help="Video file or live URL to inspect.")],
    config: control_cli.BackendConfigOption = None,
    endpoint: control_cli.EndpointOption = None,
    token_file: control_cli.TokenFileOption = None,
    ca_file: control_cli.CaFileOption = None,
    as_json: control_cli.JsonOption = False,
) -> None:
    """Show how a source file will be handled (codec, size, fps, chosen mode).

    Runs on this machine. With --endpoint, $VCAM_ENDPOINT or --config it runs on
    that engine instead, resolving the source there; it never falls back to local.
    """
    remote = control_cli.backend(config, endpoint, token_file, ca_file)
    if remote is not None:
        control_cli.probe_remote(remote, source, as_json)
        return
    try:
        camera = CameraSpec(name="probe", source=source)
        info = probe_source(
            camera.source, timeout=camera.source_timeout if camera.is_live else 20.0
        )
    except (ProbeError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    fields = asdict(info)
    del fields["path"]
    fields["auto_mode"] = resolve_mode(camera, info).value
    if as_json:
        control_cli._emit(fields)
    else:
        control_cli.print_probe(display_source(camera.source), fields)


@app.command()
def doctor(
    mediamtx_binary: Annotated[
        Path | None, typer.Option("--mediamtx-binary", help="Check this MediaMTX binary.")
    ] = None,
    mediamtx_version: Annotated[
        str,
        typer.Option("--mediamtx-version", help="Release to look for in the local cache."),
    ] = binaries.DEFAULT_VERSION,
    config: control_cli.BackendConfigOption = None,
    endpoint: control_cli.EndpointOption = None,
    token_file: control_cli.TokenFileOption = None,
    ca_file: control_cli.CaFileOption = None,
    as_json: control_cli.JsonOption = False,
) -> None:
    """Check that ffmpeg, ffprobe and MediaMTX are available. Never downloads.

    Runs on this machine. With --endpoint, $VCAM_ENDPOINT or --config it checks
    the host of that engine instead; it never falls back to this machine.
    """
    remote = control_cli.backend(config, endpoint, token_file, ca_file)
    if remote is not None:
        if mediamtx_binary is not None or mediamtx_version != binaries.DEFAULT_VERSION:
            raise _fail(
                "--mediamtx-binary and --mediamtx-version describe this machine, "
                "so they cannot be used with a remote engine",
                2,
            )
        control_cli.doctor_remote(remote, as_json)
        return

    ok = True
    tools: dict[str, bool] = {}

    def say(line: str) -> None:
        if not as_json:
            console.print(line)

    for tool in ("ffmpeg", "ffprobe"):
        location = shutil.which(tool)
        tools[tool] = location is not None
        if location:
            say(f"[green]OK[/]   {tool}: {location}")
        else:
            ok = False
            say(f"[red]MISS[/] {tool}: not on PATH")

    try:
        binary = binaries.resolve_binary(
            mediamtx_binary, version=mediamtx_version, allow_download=False
        )
        tools["mediamtx"] = True
        say(f"[green]OK[/]   mediamtx: {binary}")
    except binaries.BinaryError:
        tools["mediamtx"] = False
        say(
            f"[yellow]WARN[/] mediamtx: not installed "
            f"(will download {mediamtx_version} on first run, or use `vcam install-server`)"
        )

    try:
        say(f"[dim]platform: {binaries.platform_slug()}[/]")
    except binaries.BinaryError as exc:
        ok = False
        say(f"[red]MISS[/] {exc}")

    # Capture replay is optional: report it, but never fail the check on it.
    pcap: str | None = None
    try:
        pcap = pcap_backend_version()
        say(f"[green]OK[/]   pcap backend: {pcap}")
    except PcapError as exc:
        say(f"[yellow]WARN[/] pcap backend: {exc}")

    if as_json:
        # `ok` is the exit status: here MediaMTX is optional because it is downloaded on first run.
        control_cli._emit({"ok": ok, "tools": tools, "pcap_backend": pcap})
    else:
        _print_toxiproxy_status()

    if not ok:
        raise typer.Exit(1)


def _print_toxiproxy_status() -> None:
    # Chaos testing (Toxiproxy) is optional: report CLI and API if reachable.
    toxi_cli = shutil.which("toxiproxy-cli")
    toxi_api_running = False
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8474/version", timeout=0.5) as resp:
            if resp.status == 200:
                toxi_api_running = True
    except Exception:
        pass

    if toxi_cli or toxi_api_running:
        status_parts = []
        if toxi_cli:
            status_parts.append(f"cli: {toxi_cli}")
        if toxi_api_running:
            status_parts.append("api: http://127.0.0.1:8474")
        joined = ", ".join(status_parts)
        console.print(f"[green]OK[/]   toxiproxy: {joined} (optional chaos testing)")
    else:
        console.print(
            "[dim]INFO  toxiproxy: not detected (optional for TCP chaos testing; "
            "run `docker compose --profile chaos up -d`)[/]"
        )


@app.command("install-server")
def install_server(
    version: Annotated[
        str,
        typer.Option(
            "--mediamtx-version",
            help="Release to download (same flag name as `run` and `doctor`).",
        ),
    ] = binaries.DEFAULT_VERSION,
    force: Annotated[bool, typer.Option("--force", help="Re-download even if cached.")] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify-checksum/--no-verify-checksum",
            help="Verify the SHA-256 checksum of the download.",
        ),
    ] = True,
) -> None:
    """Download the MediaMTX binary into the local cache.

    Unrelated to `vcam service install`, which registers vcam itself as a
    background service. This command only fetches the RTSP server binary.
    """
    target = binaries.install_path(version)
    if target.is_file() and not force:
        console.print(f"already installed: [bold]{target}[/]")
        return
    try:
        path = binaries.download(
            version=version,
            verify=verify,
            on_event=lambda message: console.print(f"[dim]{message}[/]"),
        )
    except binaries.BinaryError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"installed [bold]{path}[/]")


# ---------------------------------------------------------------------------
# service management (systemd user unit / launchd agent)
# ---------------------------------------------------------------------------

service_app = typer.Typer(
    help=(
        "Run vcam as a persistent background service.\n\n"
        "Linux: a systemd user unit (no root needed).\n"
        "macOS: a launchd LaunchAgent."
    ),
    no_args_is_help=True,
)
app.add_typer(service_app, name="service")
control_cli.register(app)

_NameOption = Annotated[
    str,
    typer.Option("--name", "-n", help="Service / unit name."),
]


@service_app.command()
def install(
    config: ConfigOption = None,
    name: _NameOption = "vcam",
) -> None:
    """Install vcam as a background service, start it, and enable it at login.

    Bakes the resolved config path into the unit so the service is
    self-contained.  Put all camera settings in the YAML file; edit the unit
    afterwards if you need extra CLI flags.

    Not to be confused with `vcam install-server`, which downloads MediaMTX.
    """
    path = config or find_default_config()
    if path is None:
        raise _fail("no config file found; pass --config or create one with `vcam init`")
    try:
        stack = load_stack(path, allow_empty=True)
        if not stack.control.enabled and not stack.cameras and not stack.replays:
            raise ConfigError(f"{path} declares no cameras or replays")
        if stack.control.enabled:
            from importlib.util import find_spec

            if find_spec("fastapi") is None or find_spec("uvicorn") is None:
                raise ConfigError(
                    "install VCAM with the control extra before enabling this service"
                )
            if stack.control.token_file is not None:
                from .control import read_token_file

                read_token_file(stack.control.token_file)
    except (ConfigError, SupervisorError) as exc:
        raise _fail(str(exc)) from exc
    try:
        summary = _service.install(name, Path(path).expanduser().resolve())
    except ServiceError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"[green]installed[/] {summary}")


@service_app.command()
def start(name: _NameOption = "vcam") -> None:
    """Start a previously installed service."""
    try:
        summary = _service.start(name)
    except ServiceError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"[green]started[/] {summary}")


@service_app.command()
def stop(name: _NameOption = "vcam") -> None:
    """Stop the running service (unit file is kept)."""
    try:
        summary = _service.stop(name)
    except ServiceError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"[yellow]stopped[/] {summary}")


@service_app.command()
def status(name: _NameOption = "vcam") -> None:
    """Show whether the service is installed and running.

    Exits 0 when the service is running, 3 when it is stopped or not installed.
    """
    try:
        svc_status = _service.status(name)
    except ServiceError as exc:
        raise _fail(str(exc)) from exc
    style = "green" if svc_status.active else ("yellow" if svc_status.installed else "red")
    console.print(f"[{style}]{svc_status.line}[/]")
    if not svc_status.active:
        raise typer.Exit(3)


@service_app.command()
def uninstall(name: _NameOption = "vcam") -> None:
    """Stop, disable, and remove the unit / plist file."""
    try:
        summary = _service.uninstall(name)
    except ServiceError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"[red]removed[/] {summary}")


@service_app.command()
def logs(name: _NameOption = "vcam") -> None:
    """Stream the service log (journalctl on Linux, tail on macOS)."""
    try:
        _service.tail_logs(name)
    except ServiceError as exc:
        raise _fail(str(exc)) from exc
    except KeyboardInterrupt:
        pass


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
