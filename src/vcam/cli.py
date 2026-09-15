"""Typer command line interface for the virtual RTSP camera tool."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import __version__, binaries
from . import service as _service
from .cli_helpers import OverridesResolver, RunContext
from .config import (
    dump_stack,
    example_stack,
    find_default_config,
    format_validation_error,
    load_stack,
    save_stack,
)
from .errors import (
    ConfigError,
    NTPError,
    PcapError,
    ProbeError,
    ServiceError,
    SupervisorError,
)
from .ffmpeg import build_publish_command, resolve_mode
from .mediamtx import plan_instances, render_server_config_yaml
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
    SimulationSpec,
    StreamMode,
    Transport,
    VideoCodec,
    VideoSettings,
)
from .ntp import apply_offset, has_sys_time_cap, measure_offset, running_in_container
from .pcap import backend_version as pcap_backend_version
from .probe import probe as probe_source
from .probe import try_probe
from .supervisor import REPLAY_PASSWORD_ENV, CameraRuntime, Supervisor

console = Console()
error_console = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Serve local video files as looping virtual RTSP cameras.\n\n"
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


def _parse_camera_argument(value: str) -> tuple[str, Path]:
    """Parse ``NAME=/path/to/file.mp4`` (or a bare path) into a camera tuple."""
    if "=" in value:
        name, _, source = value.partition("=")
        name = name.strip()
        source = source.strip()
        if not name or not source:
            raise typer.BadParameter(f"expected NAME=PATH, got {value!r}")
        return name, Path(source).expanduser()
    path = Path(value).expanduser()
    return _slug(path.stem), path


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
    except ValueError as exc:
        raise _fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    config: ConfigOption = None,
    source: Annotated[
        Path | None,
        typer.Option("--source", "-s", help="Video file to serve as a single camera."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Camera name for --source (default: file stem)."),
    ] = None,
    camera: Annotated[
        list[str] | None,
        typer.Option(
            "--camera",
            help="Repeatable NAME=PATH pair, e.g. --camera cam1=videos/a.mp4.",
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
            help="auto: copy when the source is H.264/HEVC, else transcode.",
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
                "Sync the container clock to this NTP server before starting "
                "(e.g. 192.168.198.151). Docker only: rejected on bare CLI / "
                "service deployments, and measured but not applied without "
                "cap_add: [SYS_TIME]."
            ),
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Start the RTSP server and publish every camera. Runs until interrupted.

    With no --config/--source/--camera, the first of cameras.yaml, cameras.yml,
    vcam.yaml or vcam.yml in the current directory is used. Per-camera options
    below override the config for *every* camera in the stack.
    """
    stack = _build_stack(config=config, source=source, name=name, cameras=camera)

    try:
        _apply_overrides(
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
            video=_video_overrides(resolution, fps, bitrate, codec, encoder, gop, preset),
            simulation=_simulation_overrides(
                simulation,
                noise_level,
                simulation_interval,
                simulation_duration,
                simulation_filters,
            ),
        )
    except ValidationError as exc:
        raise _fail(f"invalid options:\n{format_validation_error(exc)}") from exc

    if not stack.enabled_cameras:
        raise _fail("no enabled cameras to serve")

    if dry_run:
        _print_dry_run(stack, ffmpeg_log_level)
        return

    _setup_logging(verbose)

    _run_ntp_sync(stack.server.ntp_server)

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
    )

    try:
        raise typer.Exit(supervisor.run())
    except SupervisorError as exc:
        supervisor.shutdown()
        raise _fail(str(exc)) from exc
    except KeyboardInterrupt:
        supervisor.shutdown()
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _build_stack(
    *,
    config: Path | None,
    source: Path | None,
    name: str | None,
    cameras: list[str] | None,
) -> CameraStack:
    inline: list[CameraSpec] = []

    if source is not None:
        inline.append(CameraSpec(name=name or _slug(source.stem), source=Path(source).expanduser()))
    for entry in cameras or []:
        camera_name, camera_source = _parse_camera_argument(entry)
        inline.append(CameraSpec(name=camera_name, source=camera_source))

    if inline and config is not None:
        raise _fail("use either --config or --source/--camera, not both")

    if inline:
        try:
            return CameraStack(cameras=inline)
        except ValidationError as exc:
            raise _fail(f"invalid camera definition:\n{format_validation_error(exc)}") from exc

    return _load_stack_or_exit(config)


def _run_ntp_sync(ntp_server: str | None) -> None:
    """Query *ntp_server* and apply the measured offset. Container-only."""
    if ntp_server is None:
        return

    logger = logging.getLogger("vcam")

    if not running_in_container():
        raise _fail(
            f"--ntp-server ({ntp_server}) is only supported when running inside a Docker "
            "container.\nApplying it on a bare CLI or systemd service would skew the host "
            "system clock.\nRemove --ntp-server, or run vcam inside Docker with "
            "cap_add: [SYS_TIME]."
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
        console.print(render_server_config_yaml(instance, stack.server).rstrip())
        console.print()

    console.print("[bold]# publishers[/]")
    for camera in stack.enabled_cameras:
        info = try_probe(camera.source) if camera.source.is_file() else None
        command = build_publish_command(
            camera,
            stack.publish_url(camera),
            info=info,
            log_level=ffmpeg_log_level,
        )
        console.print(f"[dim]# {camera.name} -> {stack.read_url(camera)}[/]")
        console.print(" ".join(_quote(part) for part in command))
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
        table.add_column("sim")
        table.add_column("source", style="dim")
        for runtime in runtimes:
            table.add_row(
                runtime.camera.name,
                runtime.read_url_with_credentials,
                runtime.mode.value,
                _simulation_label(runtime.camera),
                str(runtime.camera.source),
            )
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
    table.add_column("loop")
    table.add_column("offset")
    table.add_column("source", style="dim")
    for camera in stack.cameras:
        table.add_row(
            camera.name if camera.enabled else f"[strike]{camera.name}[/]",
            stack.read_url(camera, host),
            camera.mode.value,
            _simulation_label(camera),
            "yes" if camera.loop else "no",
            f"{camera.start_offset:g}s",
            str(camera.source),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# config management
# ---------------------------------------------------------------------------


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
    if path.exists() and not force:
        raise _fail(f"{path} already exists (use --force to overwrite)")
    stack = example_stack([Path(item).expanduser() for item in source] if source else None)
    save_stack(stack, path)
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
    if path.exists() and not force:
        raise _fail(f"{path} already exists (use --force to overwrite)")

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
        console.print(f"  [bold]{cam.name}[/]  ({cam.mode.value})  ← {cam.source.name}")
    console.print()

    save_stack(stack, path)
    console.print(f"wrote [bold]{path}[/]\n")
    console.print(
        f"Run [bold]vcam run --config {path}[/] to start, "
        "or [bold]vcam list --config " + str(path) + "[/] to review."
    )


@app.command()
def add(
    source: Annotated[Path, typer.Argument(help="Video file to add as a camera.")],
    config: ConfigOption = None,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Camera name (default: file stem).")
    ] = None,
    mode: Annotated[
        StreamMode,
        typer.Option(
            "--mode",
            help="auto: copy when the source is H.264/HEVC, else transcode.",
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
) -> None:
    """Append a camera to an existing configuration file.

    Only the most common per-camera settings are exposed here; edit the YAML
    directly for the rest (audio, transport, realtime, preset, simulation timing).
    """
    path = config or find_default_config()
    if path is None:
        raise _fail("no config file found. Create one with `vcam init`.")

    try:
        stack = load_stack(path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc

    overrides = _video_overrides(resolution, fps, bitrate, codec, encoder, gop, None)
    try:
        stack.cameras.append(
            CameraSpec(
                name=name or _slug(source.stem),
                # Stored absolute: relative paths in a config resolve against the
                # config directory, which is rarely the cwd `add` was run from.
                source=Path(source).expanduser().resolve(),
                mode=mode,
                start_offset=start_offset,
                port=port,
                video=VideoSettings(**overrides),  # type: ignore[arg-type]
                simulation=SimulationSpec(mode=simulation),
            )
        )
        CameraStack.model_validate(stack.model_dump())
    except ValidationError as exc:
        raise _fail(f"invalid camera definition:\n{format_validation_error(exc)}") from exc

    save_stack(stack, path)
    console.print(f"added [bold]{stack.cameras[-1].name}[/] to {path}")


@app.command("list")
def list_cameras(config: ConfigOption = None, host: DisplayHostOption = None) -> None:
    """Show the cameras and replays declared in a configuration file."""
    stack = _load_stack_or_exit(config)
    _print_camera_table(stack, host)
    _print_replay_table(stack, host)


@app.command()
def urls(
    config: ConfigOption = None,
    host: DisplayHostOption = None,
    all_cameras: Annotated[bool, typer.Option("--all", help="Include disabled cameras.")] = False,
) -> None:
    """Print one RTSP URL per line (handy for scripts and pipelines)."""
    stack = _load_stack_or_exit(config)
    cameras = stack.cameras if all_cameras else stack.enabled_cameras
    for camera in cameras:
        print(stack.read_url(camera, host))
    replays = stack.replays if all_cameras else stack.enabled_replays
    for replay in replays:
        print(stack.replay_url(replay, host))


@app.command()
def show(config: ConfigOption = None) -> None:
    """Print the fully resolved configuration as YAML."""
    stack = _load_stack_or_exit(config)
    console.print(dump_stack(stack).rstrip())


@app.command("clock-status")
def clock_status(
    ntp_server: Annotated[
        str | None,
        typer.Option(
            "--ntp-server",
            help=(
                "NTP server to measure the clock offset against "
                "(e.g. 192.168.198.151). Read-only: never adjusts the clock."
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
            "be applied.\nAdd [bold]cap_add: [SYS_TIME][/] to docker-compose.yml."
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
    source: Annotated[Path, typer.Argument(help="Video file to inspect.")],
) -> None:
    """Show how a source file will be handled (codec, size, fps, chosen mode)."""
    try:
        info = probe_source(Path(source).expanduser())
    except ProbeError as exc:
        raise _fail(str(exc)) from exc

    camera = CameraSpec(name="probe", source=Path(source).expanduser())

    table = Table(title=str(info.path), title_justify="left")
    table.add_column("property", style="bold")
    table.add_column("value")
    table.add_row("codec", info.codec or "-")
    table.add_row("resolution", info.resolution or "-")
    table.add_row("fps", f"{info.fps:.3f}" if info.fps else "-")
    table.add_row("duration", f"{info.duration:.2f}s" if info.duration else "-")
    table.add_row("bitrate", f"{info.bitrate // 1000} kbit/s" if info.bitrate else "-")
    table.add_row("pixel format", info.pix_fmt or "-")
    table.add_row("audio track", "yes" if info.has_audio else "no")
    table.add_row("auto mode", resolve_mode(camera, info).value)
    console.print(table)


@app.command()
def doctor(
    mediamtx_binary: Annotated[
        Path | None, typer.Option("--mediamtx-binary", help="Check this MediaMTX binary.")
    ] = None,
    mediamtx_version: Annotated[
        str,
        typer.Option("--mediamtx-version", help="Release to look for in the local cache."),
    ] = binaries.DEFAULT_VERSION,
) -> None:
    """Check that ffmpeg, ffprobe and MediaMTX are available. Never downloads."""
    ok = True

    for tool in ("ffmpeg", "ffprobe"):
        location = shutil.which(tool)
        if location:
            console.print(f"[green]OK[/]   {tool}: {location}")
        else:
            ok = False
            console.print(f"[red]MISS[/] {tool}: not on PATH")

    try:
        binary = binaries.resolve_binary(
            mediamtx_binary, version=mediamtx_version, allow_download=False
        )
        console.print(f"[green]OK[/]   mediamtx: {binary}")
    except binaries.BinaryError:
        console.print(
            f"[yellow]WARN[/] mediamtx: not installed "
            f"(will download {mediamtx_version} on first run, or use `vcam install-server`)"
        )

    try:
        console.print(f"[dim]platform: {binaries.platform_slug()}[/]")
    except binaries.BinaryError as exc:
        ok = False
        console.print(f"[red]MISS[/] {exc}")

    # Capture replay is optional: report it, but never fail the check on it.
    try:
        console.print(f"[green]OK[/]   pcap backend: {pcap_backend_version()}")
    except PcapError as exc:
        console.print(f"[yellow]WARN[/] pcap backend: {exc}")

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

    if not ok:
        raise typer.Exit(1)


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
        load_stack(path)
    except ConfigError as exc:
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
