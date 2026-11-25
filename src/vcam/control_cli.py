"""CLI commands that drive a running ``vcam run --control`` instance.

Every command here talks to the control API. Discovery finds the instance
that owns a config file through its private descriptor and proves it is the
same process (instance id and config path) before sending anything. An
explicit ``--endpoint`` (or ``VCAM_ENDPOINT``) never falls back to editing YAML
offline or running a diagnostic locally, and is never combined with a local
config file.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import find_default_config
from .control_client import ControlClient, ControlClientError
from .errors import SupervisorError
from .sources import display_source, parse_source

console = Console()
error_console = Console(stderr=True)

# The only control API major this client speaks (`api_version` in GET /status).
SUPPORTED_API_VERSION = 1
TOKEN_FILE_ENV = "VCAM_CONTROL_TOKEN_FILE"
# The first of these that is set and non-empty names an explicit endpoint.
ENDPOINT_ENVS = ("VCAM_ENDPOINT", "VCAM_CONTROL_ENDPOINT")


def _fail(message: str, code: int = 1) -> typer.Exit:
    # Server messages and error codes contain brackets that Rich would eat as markup.
    error_console.print(f"[bold red]error:[/] {escape(message)}")
    return typer.Exit(code)


ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="YAML config the running instance owns."),
]
# Host diagnostics describe a machine, so a config only selects an engine when given explicitly.
BackendConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help=(
            "Run on the engine that owns this YAML config (a running `vcam run --control`) "
            "instead of on this machine; it never falls back to this machine."
        ),
    ),
]
EndpointOption = Annotated[
    str | None,
    typer.Option(
        "--endpoint",
        help=(
            "Control API URL of a remote engine (default: $VCAM_ENDPOINT or "
            "$VCAM_CONTROL_ENDPOINT). Skips discovery, never falls back to local files, "
            "and needs --token-file."
        ),
    ),
]
TokenFileOption = Annotated[
    Path | None,
    typer.Option(
        "--token-file",
        envvar=TOKEN_FILE_ENV,
        help="Private file holding the bearer token (never pass the token itself).",
    ),
]
CaFileOption = Annotated[
    Path | None,
    typer.Option("--ca-file", help="CA bundle that signs an HTTPS control endpoint."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print raw JSON.")]
WaitOption = Annotated[
    bool,
    typer.Option("--wait/--no-wait", help="Wait for the operation to finish (default: wait)."),
]
PortOption = Annotated[
    int | None,
    typer.Option("--port", "-p", help="RTSP port of the camera (needed if the name repeats)."),
]
DisruptionOption = Annotated[
    bool,
    typer.Option(
        "--allow-disruption",
        "--yes",
        help="Acknowledge that every stream restarts or stops.",
    ),
]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class Remote:
    """A verified connection to one controlled vcam instance."""

    def __init__(
        self, client: ControlClient, status: dict[str, Any], *, explicit: bool = False
    ) -> None:
        self.client = client
        self.status = status
        # The caller named this endpoint, so it may be another host: local paths
        # and files mean nothing there and must reach it exactly as typed.
        self.explicit = explicit

    @property
    def ephemeral(self) -> bool:
        """An instance started without a config file: every change is runtime-only."""
        config = self.status.get("config")
        return isinstance(config, dict) and config.get("path") is None

    def get(self, path: str) -> Any:
        return self.client.request("GET", path)

    def write(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        revision: str | None = None,
        wait: bool = True,
        timeout: float = 120,
    ) -> dict[str, Any]:
        """Submit a write and (by default) wait for its definitive outcome."""
        op = self.client.request(
            method, path, payload, revision=revision, idempotency_key=uuid.uuid4().hex
        )
        if not wait or not isinstance(op, dict) or "id" not in op:
            return op
        return self.client.wait(op["id"], timeout=timeout)


def _ssl_context(ca_file: Path | str | None) -> ssl.SSLContext | None:
    if ca_file is None:
        return None
    try:
        return ssl.create_default_context(cafile=str(ca_file))
    except (OSError, ssl.SSLError) as exc:
        raise _fail(f"cannot load CA file {ca_file}: {exc}") from exc


def _read_token(path: Path) -> str:
    from .control import read_token_file

    try:
        return read_token_file(path)
    except SupervisorError as exc:
        raise _fail(f"{exc} ({path})") from exc


def _verified_status(status: Any, where: str) -> dict[str, Any]:
    """Accept only a status from a control API this client can safely drive.

    Anything uncertain (wrong shape, no version, another major, no identity) is
    an error: a command must never reach a server we cannot identify.
    """
    if not isinstance(status, dict):
        raise _fail(f"{where} did not answer like a vcam control API; refusing to send commands")
    version = status.get("api_version")
    if type(version) is not int:
        raise _fail(f"{where} did not report a control API version; refusing to send commands")
    if version != SUPPORTED_API_VERSION:
        raise _fail(
            f"{where} speaks control API v{version}; this vcam supports "
            f"v{SUPPORTED_API_VERSION}. Use a matching vcam.",
            2,
        )
    instance = status.get("instance_id")
    if not isinstance(instance, str) or not instance:
        raise _fail(f"{where} did not identify its instance; refusing to send commands")
    return status


def _same_path(reported: Any, expected: Path) -> bool:
    if not isinstance(reported, str) or not reported:
        return False
    try:
        return Path(reported).resolve() == expected
    except (OSError, RuntimeError, ValueError):
        return False


def discover(config: Path | None, *, required: bool) -> Remote | None:
    """Connect to the instance that controls ``config``.

    Returns None (when not ``required``) only if no live instance claims the
    config: then offline YAML edits are safe. A claimed but unreachable or
    mismatched instance is always an error, never an offline fallback.
    """
    from .control import pid_alive, read_descriptor

    path = config or find_default_config()
    if path is None:
        if required:
            raise _fail("no config file found. Pass --config or --endpoint.")
        return None
    path = path.resolve()
    try:
        descriptor = read_descriptor(path)
    except SupervisorError as exc:
        raise _fail(str(exc)) from exc
    if descriptor is None or not pid_alive(descriptor.get("pid")):
        if required:
            raise _fail(f"no running `vcam run --control` instance owns {path}")
        return None

    token = _read_token(Path(descriptor["token_file"]))
    client = _client(descriptor["endpoint"], token, descriptor.get("ca_file"))
    where = f"the endpoint advertised for {path}"
    status = _verified_status(_call(lambda: client.request("GET", "status")), where)
    config_status = status.get("config")
    if status["instance_id"] != descriptor["instance_id"] or not _same_path(
        config_status.get("path") if isinstance(config_status, dict) else None, path
    ):
        raise _fail(f"{where} is not the instance that owns it; refusing to send commands")
    return Remote(client, status)


def _client(endpoint: str, token: str, ca_file: Path | str | None) -> ControlClient:
    try:
        return ControlClient(endpoint, token=token, ssl_context=_ssl_context(ca_file))
    except ValueError as exc:
        raise _fail(str(exc)) from exc


def _explicit_endpoint(endpoint: str | None) -> tuple[str, str] | None:
    """The endpoint the caller named and where it came from, or None to use discovery.

    ``--endpoint`` wins over the environment. An empty variable names nothing,
    like an unset one, but an empty ``--endpoint ""`` is an explicit (invalid)
    choice. This is resolved here rather than by the option parser so the origin
    is known exactly and no parser internals are needed.
    """
    if endpoint is not None:
        return endpoint, "--endpoint"
    for name in ENDPOINT_ENVS:
        value = os.environ.get(name)
        if value:
            return value, f"${name}"
    return None


def _reject_local_selectors(config: Path | None, offline: bool, origin: str) -> None:
    """An explicit endpoint names its own engine, so local files and modes cannot apply."""
    remedy = f"unset {origin[1:]}" if origin.startswith("$") else "drop --endpoint"
    for given, option in ((offline, "--offline"), (config is not None, "--config")):
        if given:
            raise _fail(
                f"{option} cannot be combined with an explicit endpoint ({origin}), which never "
                f"uses local files; drop {option} or {remedy}",
                2,
            )


def _connect_endpoint(endpoint: str, token_file: Path | None, ca_file: Path | None) -> Remote:
    if not endpoint.strip():
        raise _fail("the control endpoint is empty", 2)
    if token_file is None:
        raise _fail(f"an explicit endpoint needs --token-file (or ${TOKEN_FILE_ENV})", 2)
    client = _client(endpoint, _read_token(token_file), ca_file)
    status = _verified_status(
        _call(lambda: client.request("GET", "status")), f"the endpoint {client.base_url}"
    )
    return Remote(client, status, explicit=True)


def target(
    config: Path | None,
    endpoint: str | None,
    token_file: Path | None,
    ca_file: Path | None,
    *,
    offline: bool = False,
) -> Remote | None:
    """Choose the engine a command that can also work offline should talk to.

    An explicit endpoint always wins and never falls back to anything local.
    None means no live owner holds the local config (or ``--offline``), so the
    caller may act on the YAML file.
    """
    named = _explicit_endpoint(endpoint)
    if named is not None:
        url, origin = named
        _reject_local_selectors(config, offline, origin)
        return _connect_endpoint(url, token_file, ca_file)
    if offline:
        return None
    return discover(config, required=False)


def connect(
    config: Path | None,
    endpoint: str | None,
    token_file: Path | None,
    ca_file: Path | None,
) -> Remote:
    """Connect for a command that has no offline form: failing to find an engine is an error."""
    named = _explicit_endpoint(endpoint)
    if named is not None:
        url, origin = named
        _reject_local_selectors(config, False, origin)
        return _connect_endpoint(url, token_file, ca_file)
    remote = discover(config, required=True)
    assert remote is not None
    return remote


def backend(
    config: Path | None,
    endpoint: str | None,
    token_file: Path | None,
    ca_file: Path | None,
) -> Remote | None:
    """Where a host diagnostic (`probe`, `doctor`) runs: None means on this machine.

    A diagnostic describes the machine it runs on, so it stays local even when an
    engine owns the default config. Only an explicit selector (``--endpoint``,
    the environment, or ``--config``) moves it to an engine, and then it never
    falls back to running locally.
    """
    if config is None and _explicit_endpoint(endpoint) is None:
        return None
    return connect(config, endpoint, token_file, ca_file)


def _call(fn: Any) -> Any:
    try:
        return fn()
    except ControlClientError as exc:
        raise _client_failure(exc) from exc


def _client_failure(exc: ControlClientError) -> typer.Exit:
    if exc.code in {"disruption_not_acknowledged", "full_apply_required"}:
        hint = (
            "rerun with --allow-disruption"
            if exc.code == "disruption_not_acknowledged"
            else "preview it with `vcam config plan FILE`, then `vcam config apply FILE`"
        )
        return _fail(f"{exc}; {hint}", 2)
    message = f"{exc} [{exc.code}]"
    details = exc.details
    if details.get("config_persisted") and not details.get("applied"):
        message += (
            "\nthe change was saved to the config file but is NOT running; "
            "fix the cause, then retry with `vcam config reload --apply` "
            "(or `vcam camera start NAME` for one camera)"
        )
    extra = {k: v for k, v in details.items() if k not in {"config_persisted", "applied"}}
    if extra:
        message += "\n" + json.dumps(extra, indent=2, default=str)
    return _fail(message)


@contextmanager
def _handled() -> Iterator[None]:
    try:
        yield
    except ControlClientError as exc:
        raise _client_failure(exc) from exc


def _emit(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    # Table cells are Rich markup; URLs, sources and names can contain brackets.
    return escape("" if value is None else str(value))


def print_cameras(cameras: list[dict[str, Any]], title: str = "Cameras") -> None:
    table = Table(title=title, title_justify="left")
    for column in ("port", "camera", "enabled", "state", "url", "source"):
        table.add_column(column, overflow="fold")
    for camera in cameras:
        table.add_row(
            _cell(camera.get("port")),
            _cell(camera.get("name")),
            "yes" if camera.get("enabled") else "no",
            _cell(camera.get("state")),
            _cell(camera.get("url")),
            _cell(camera.get("source")),
        )
    console.print(table)


def print_replays(replays: list[dict[str, Any]], title: str = "Capture replays") -> None:
    table = Table(title=title, title_justify="left")
    for column in ("port", "replay", "enabled", "state", "url"):
        table.add_column(column, overflow="fold")
    for replay in replays:
        table.add_row(
            _cell(replay.get("port")),
            _cell(replay.get("name")),
            "yes" if replay.get("enabled") else "no",
            _cell(replay.get("state")),
            _cell(replay.get("url")),
        )
    console.print(table)


def _print_operation(remote: Remote, op: dict[str, Any], verb: str) -> None:
    status = op.get("status")
    if status == "succeeded":
        result = op.get("result") or {}
        mode = result.get("mode")
        suffix = f" ({escape(str(mode))})" if mode else ""
        if remote.ephemeral:
            suffix += " (runtime only: this instance has no config file, nothing was saved)"
        console.print(f"[green]{escape(verb)}[/]{suffix}")
    else:
        console.print(
            f"{escape(verb)}: operation {escape(str(op.get('id')))} is {escape(str(status))}"
        )


def print_added(name: str, op: dict[str, Any]) -> None:
    """Say what an online add did, from the operation's own truthful flags."""
    label = f"[bold]{escape(name)}[/]"
    status = op.get("status")
    if status != "succeeded":
        console.print(
            f"adding {label}: operation {escape(str(op.get('id')))} is {escape(str(status))}"
        )
    elif op.get("config_persisted"):
        console.print(f"added {label} to the running instance and its config")
    else:
        console.print(
            f"added {label} to the running instance (runtime only: no config file was changed)"
        )


def print_plan(plan: dict[str, Any]) -> None:
    changes = plan.get("plan") or plan
    for label, key in (("add", "added"), ("remove", "removed"), ("update", "updated")):
        for item in changes.get(key) or []:
            console.print(f"  {label:<7} {item.get('name')} (port {item.get('port')})")
    for item in changes.get("restart_required") or []:
        console.print(f"  restart {item}")
    if not any(changes.get(k) for k in ("added", "removed", "updated", "restart_required")):
        console.print("  no changes")
    if changes.get("disruptive"):
        reasons = ", ".join(changes.get("disruptive_reasons") or []) or "full restart"
        console.print(f"[yellow]disruptive:[/] {reasons}")


# ---------------------------------------------------------------------------
# Top-level commands against a running engine: list, urls, show, probe, doctor
# ---------------------------------------------------------------------------


def _items(data: Any, key: str) -> list[dict[str, Any]]:
    """The list of objects under ``key``, or a clear failure if the engine answered oddly."""
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _fail(f"the engine returned an unexpected {key} response")
    return items


def _urls_path(host: str | None, include_disabled: bool) -> str:
    query = []
    if host:
        query.append(f"host={quote(host, safe='')}")
    if include_disabled:
        query.append("include_disabled=true")
    return "urls" + ("?" + "&".join(query) if query else "")


def _readdress(cameras: list[dict[str, Any]], replays: list[dict[str, Any]], links: Any) -> None:
    """Show the listed streams under the host the caller asked for.

    The engine validates the host and builds the URLs; the client only swaps
    them in, matching streams by kind, port and name.
    """
    streams = links.get("streams") if isinstance(links, dict) else None
    if not isinstance(streams, list):
        raise _fail("the engine returned an unexpected urls response")
    addressed = {
        (stream.get("kind"), stream.get("port"), stream.get("name")): stream.get("url")
        for stream in streams
        if isinstance(stream, dict)
    }
    for kind, rows in (("camera", cameras), ("replay", replays)):
        for row in rows:
            url = addressed.get((kind, row.get("port"), row.get("name")))
            if isinstance(url, str):
                row["url"] = url


def list_online(remote: Remote, host: str | None, as_json: bool) -> None:
    """`vcam list` against a running engine: live state, not only the file."""
    with _handled():
        cameras = remote.get("cameras")
        replays = remote.get("replays")
        links = remote.get(_urls_path(host, True)) if host else None
    camera_rows = _items(cameras, "cameras")
    replay_rows = _items(replays, "replays")
    if links is not None:
        _readdress(camera_rows, replay_rows, links)
    if as_json:
        _emit({"revision": cameras.get("revision"), "cameras": camera_rows, "replays": replay_rows})
        return
    print_cameras(camera_rows)
    if replay_rows:
        print_replays(replay_rows)


def urls_online(remote: Remote, host: str | None, all_cameras: bool, as_json: bool) -> None:
    """`vcam urls` against a running engine. These URLs never carry credentials."""
    with _handled():
        data = remote.get(_urls_path(host, all_cameras))
    urls = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise _fail("the engine returned an unexpected urls response")
    if as_json:
        _emit(data)
        return
    for url in urls:
        print(url)


def show_online(remote: Remote, as_json: bool) -> None:
    """`vcam show` against a running engine (redacted: raw YAML is never served)."""
    with _handled():
        data = remote.get("config")
    if not isinstance(data, dict) or not isinstance(data.get("effective"), dict):
        raise _fail("the engine returned an unexpected config response")
    if as_json:
        _emit(data)
    else:
        print_config(data)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) or None


def print_probe(title: str, info: dict[str, Any]) -> None:
    """Render media properties in the diagnostics API's shape (local and remote probes)."""
    width, height = info.get("width"), info.get("height")
    fps, duration, bitrate = (_number(info.get(key)) for key in ("fps", "duration", "bitrate"))
    table = Table(title=escape(title), title_justify="left")
    table.add_column("property", style="bold")
    table.add_column("value")
    table.add_row("codec", _cell(info.get("codec") or "-"))
    table.add_row("resolution", f"{width}x{height}" if width and height else "-")
    table.add_row("fps", f"{fps:.3f}" if fps else "-")
    table.add_row("duration", f"{duration:.2f}s" if duration else "-")
    table.add_row("bitrate", f"{int(bitrate) // 1000} kbit/s" if bitrate else "-")
    table.add_row("pixel format", _cell(info.get("pix_fmt") or "-"))
    table.add_row("audio track", "yes" if info.get("has_audio") else "no")
    table.add_row("auto mode", _cell(info.get("auto_mode") or "-"))
    console.print(table)


def probe_remote(remote: Remote, source: str, as_json: bool) -> None:
    """Probe ``source`` on the engine. It is resolved there, never against this machine."""
    if not source.strip():
        raise _fail("the source is empty", 2)
    with _handled():
        info = remote.client.request("POST", "diagnostics/probe", {"source": source})
    if not isinstance(info, dict):
        raise _fail("the engine returned an unexpected probe response")
    if as_json:
        _emit(info)
        return
    try:
        title = display_source(source)
    except ValueError:
        title = "source"
    print_probe(title, info)


def doctor_remote(remote: Remote, as_json: bool) -> None:
    """Check the engine's own tools. Exits 1 when the engine reports a problem."""
    with _handled():
        report = remote.client.request("POST", "diagnostics/doctor", {})
    tools = report.get("tools") if isinstance(report, dict) else None
    if not isinstance(tools, dict) or not isinstance(report.get("ok"), bool):
        raise _fail("the engine returned an unexpected doctor response")
    if as_json:
        _emit(report)
    else:
        console.print(f"[dim]checked on the engine at {escape(remote.client.base_url)}[/]")
        for tool in ("ffmpeg", "ffprobe", "mediamtx"):
            if tools.get(tool) is True:
                console.print(f"[green]OK[/]   {tool}: found on the engine")
            else:
                console.print(f"[red]MISS[/] {tool}: not found on the engine")
        pcap = report.get("pcap_backend")
        if pcap:
            console.print(f"[green]OK[/]   pcap backend: {escape(str(pcap))}")
        else:
            console.print("[yellow]WARN[/] pcap backend: not available on the engine")
    if not report["ok"]:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Camera lookup
# ---------------------------------------------------------------------------


def _find_camera(remote: Remote, name: str, port: int | None) -> tuple[dict[str, Any], str]:
    with _handled():
        listing = remote.get("cameras")
    matches = [
        c for c in listing["cameras"] if c["name"] == name and (port is None or c["port"] == port)
    ]
    if not matches:
        where = f" on port {port}" if port is not None else ""
        raise _fail(f"no camera named {name!r}{where}")
    if len(matches) > 1:
        ports = ", ".join(str(c["port"]) for c in matches)
        raise _fail(f"{name!r} exists on several ports ({ports}); pass --port")
    return matches[0], listing["revision"]


def _camera_path(camera: dict[str, Any], suffix: str = "") -> str:
    from urllib.parse import quote

    return f"cameras/{camera['port']}/{quote(camera['name'], safe='')}{suffix}"


def _parse_assignment(value: str) -> tuple[list[str], Any]:
    key, sep, raw = value.partition("=")
    if not sep or not key:
        raise _fail(f"expected KEY=VALUE, got {value!r}")
    try:
        parsed = yaml.safe_load(raw) if raw else ""
    except yaml.YAMLError as exc:
        raise _fail(f"invalid value for {key}: {exc}") from exc
    return key.split("."), parsed


def build_patch(sets: list[str], unsets: list[str]) -> dict[str, Any]:
    """Turn ``a.b=value`` assignments into an RFC 7396 merge patch."""
    patch: dict[str, Any] = {}
    items = [_parse_assignment(v) for v in sets] + [(k.split("."), None) for k in unsets]
    for path, value in items:
        node = patch
        for part in path[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise _fail(f"conflicting assignments for {'.'.join(path)}")
            node = child
        node[path[-1]] = value
    return patch


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show the state of a running controlled instance."""
    remote = connect(config, endpoint, token_file, ca_file)
    data = remote.status
    if as_json:
        _emit(data)
        return
    cfg = data.get("config") or {}
    counts = data.get("counts") or {}
    console.print(f"instance  {data.get('instance_id')} (pid {data.get('pid')})")
    console.print(f"engine    {(data.get('engine') or {}).get('state')}")
    console.print(f"config    {cfg.get('path') or '(none: ephemeral, changes are runtime-only)'}")
    console.print(f"revision  {str(cfg.get('revision'))[:12]}")
    if cfg.get("pending_reload"):
        console.print("[yellow]the config file changed on disk; run `vcam config reload`[/]")
    console.print(
        f"cameras   {counts.get('running', 0)} running of {counts.get('enabled', 0)} enabled "
        f"({counts.get('cameras', 0)} declared), "
        f"{counts.get('replays', 0)} replays"
    )
    for server in data.get("servers") or []:
        state = "running" if server.get("running") else "down"
        console.print(f"server    :{server.get('port')} {state}")


# ---------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------

camera_app = typer.Typer(
    no_args_is_help=True, help="Manage cameras on a running controlled instance."
)


@camera_app.command("list")
def camera_list(
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    as_json: JsonOption = False,
) -> None:
    """List cameras with their live state."""
    remote = connect(config, endpoint, token_file, ca_file)
    with _handled():
        data = remote.get("cameras")
    if as_json:
        _emit(data)
    else:
        print_cameras(data["cameras"])


@camera_app.command("get")
def camera_get(
    name: Annotated[str, typer.Argument(help="Camera name.")],
    port: PortOption = None,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
) -> None:
    """Print one camera as JSON (spec and runtime)."""
    remote = connect(config, endpoint, token_file, ca_file)
    camera, _ = _find_camera(remote, name, port)
    with _handled():
        _emit(remote.get(_camera_path(camera)))


@camera_app.command("add")
def camera_add(
    source: Annotated[str, typer.Argument(help="Video file or live URL.")],
    name: Annotated[str, typer.Option("--name", "-n", help="Camera name.")],
    port: Annotated[int | None, typer.Option("--port", "-p", help="Dedicated RTSP port.")] = None,
    sets: Annotated[
        list[str] | None,
        typer.Option("--set", help="Extra field as KEY=VALUE, e.g. video.fps=10 (repeatable)."),
    ] = None,
    disabled: Annotated[bool, typer.Option("--disabled", help="Add without starting.")] = False,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    wait: WaitOption = True,
    as_json: JsonOption = False,
) -> None:
    """Add a camera to the YAML and start it without touching other streams."""
    remote = connect(config, endpoint, token_file, ca_file)
    body = build_patch(sets or [], [])
    body.update({"name": name, "source": engine_source(remote, source)})
    if port is not None:
        body["port"] = port
    if disabled:
        body["enabled"] = False
    with _handled():
        revision = remote.get("cameras")["revision"]
        op = remote.write("POST", "cameras", body, revision=revision, wait=wait)
    _emit(op) if as_json else print_added(name, op)


def absolute_source(source: str) -> str:
    """Store file sources absolute: the daemon's cwd is not the caller's."""
    try:
        parsed = parse_source(source)
    except ValueError as exc:
        raise _fail(str(exc), 2) from exc
    return str(parsed.resolve()) if isinstance(parsed, Path) else source


def engine_source(remote: Remote, source: str) -> str:
    """The source as the engine must read it.

    A same-host owner shares the caller's filesystem, so a file is made absolute
    (the daemon's cwd differs). An explicit endpoint may be another machine:
    local paths mean nothing there, so the text is sent untouched and the
    engine resolves it against its own config directory.
    """
    if not remote.explicit:
        return absolute_source(source)
    if not source.strip():
        raise _fail("the source is empty", 2)
    return source


@camera_app.command("edit")
def camera_edit(
    name: Annotated[str, typer.Argument(help="Camera name.")],
    sets: Annotated[
        list[str] | None,
        typer.Option("--set", help="Field to change as KEY=VALUE (repeatable)."),
    ] = None,
    unsets: Annotated[
        list[str] | None, typer.Option("--unset", help="Field to reset to its default.")
    ] = None,
    port: PortOption = None,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    wait: WaitOption = True,
    as_json: JsonOption = False,
) -> None:
    """Change camera fields; only that camera restarts."""
    patch = build_patch(sets or [], unsets or [])
    if not patch:
        raise _fail("nothing to change; pass --set or --unset")
    remote = connect(config, endpoint, token_file, ca_file)
    camera, revision = _find_camera(remote, name, port)
    with _handled():
        op = remote.write("PATCH", _camera_path(camera), patch, revision=revision, wait=wait)
    _emit(op) if as_json else _print_operation(remote, op, f"updated {name}")


@camera_app.command("remove")
def camera_remove(
    name: Annotated[str, typer.Argument(help="Camera name.")],
    port: PortOption = None,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    wait: WaitOption = True,
    as_json: JsonOption = False,
) -> None:
    """Remove a camera from the YAML and stop only its stream."""
    remote = connect(config, endpoint, token_file, ca_file)
    camera, revision = _find_camera(remote, name, port)
    with _handled():
        op = remote.write("DELETE", _camera_path(camera), revision=revision, wait=wait)
    _emit(op) if as_json else _print_operation(remote, op, f"removed {name}")


def _camera_action_command(action: str, summary: str) -> None:
    def command(
        name: Annotated[str, typer.Argument(help="Camera name.")],
        port: PortOption = None,
        config: ConfigOption = None,
        endpoint: EndpointOption = None,
        token_file: TokenFileOption = None,
        ca_file: CaFileOption = None,
        wait: WaitOption = True,
        as_json: JsonOption = False,
    ) -> None:
        remote = connect(config, endpoint, token_file, ca_file)
        camera, revision = _find_camera(remote, name, port)
        with _handled():
            if action in {"enable", "disable"}:
                op = remote.write(
                    "PATCH",
                    _camera_path(camera),
                    {"enabled": action == "enable"},
                    revision=revision,
                    wait=wait,
                )
            else:
                op = remote.write(
                    "POST", _camera_path(camera, "/actions"), {"action": action}, wait=wait
                )
        _emit(op) if as_json else _print_operation(remote, op, f"{action} {name}")

    command.__doc__ = summary
    camera_app.command(action)(command)


_camera_action_command("enable", "Enable a camera in the YAML and start it.")
_camera_action_command("disable", "Disable a camera in the YAML and stop it.")
_camera_action_command("start", "Start a stopped camera (runtime only; YAML unchanged).")
_camera_action_command("stop", "Stop a camera until started again (runtime only).")
_camera_action_command("restart", "Restart one camera's publisher.")


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

engine_app = typer.Typer(
    no_args_is_help=True,
    help="Stop or start all streaming while the control API stays up.",
)


def _engine_command(action: str, summary: str, disruptive: bool) -> None:
    def command(
        allow_disruption: DisruptionOption = False,
        config: ConfigOption = None,
        endpoint: EndpointOption = None,
        token_file: TokenFileOption = None,
        ca_file: CaFileOption = None,
        wait: WaitOption = True,
        as_json: JsonOption = False,
    ) -> None:
        remote = connect(config, endpoint, token_file, ca_file)
        with _handled():
            op = remote.write(
                "POST",
                "engine/actions",
                {"action": action, "allow_disruption": allow_disruption},
                wait=wait,
            )
        _emit(op) if as_json else _print_operation(remote, op, f"engine {action}")

    command.__doc__ = summary + (" Needs --allow-disruption." if disruptive else "")
    engine_app.command(action)(command)


_engine_command("start", "Start every enabled camera and server.", False)
_engine_command("stop", "Stop every stream; the control API keeps running.", True)
_engine_command("restart", "Stop and start every stream.", True)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

config_app = typer.Typer(
    no_args_is_help=True, help="Inspect, preview and apply the running instance's YAML."
)


@config_app.command("show")
def config_show(
    declared: Annotated[
        bool, typer.Option("--declared", help="Show the YAML as written, without CLI overrides.")
    ] = False,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    as_json: JsonOption = False,
) -> None:
    """Print the effective configuration (secrets redacted)."""
    remote = connect(config, endpoint, token_file, ca_file)
    with _handled():
        data = remote.get("config")
    if as_json:
        _emit(data)
        return
    print_config(data, declared=declared)


def print_config(data: dict[str, Any], *, declared: bool = False) -> None:
    stack = data["declared" if declared else "effective"]
    console.print(yaml.safe_dump(stack, sort_keys=False).rstrip(), markup=False)
    overridden = (data.get("provenance") or {}).get("overridden") or []
    if overridden and not declared:
        error_console.print(f"[dim]overridden by `vcam run` options: {', '.join(overridden)}[/]")
    if data.get("pending_reload"):
        error_console.print("[yellow]the file changed on disk; run `vcam config reload`[/]")


@config_app.command("plan")
def config_plan(
    file: Annotated[
        Path | None,
        typer.Argument(help="Proposed YAML. Omit to preview the file on disk."),
    ] = None,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    as_json: JsonOption = False,
) -> None:
    """Preview what applying a config would change; nothing is modified."""
    remote = connect(config, endpoint, token_file, ca_file)
    plan = _make_plan(remote, file)
    if as_json:
        _emit(plan)
        return
    print_plan(plan)
    console.print(f"plan {plan['plan_id']} (base revision {plan['base_revision']})")


def _make_plan(remote: Remote, file: Path | None) -> dict[str, Any]:
    with _handled():
        if file is None:
            return remote.client.request("POST", "config/plan", {})
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise _fail(f"cannot read {file}: {exc.strerror or exc}") from exc
        revision = remote.get("config")["revision"]
        return remote.client.request("POST", "config/plan", {"text": text}, revision=revision)


def _apply_plan(
    remote: Remote, plan: dict[str, Any], allow_disruption: bool, wait: bool
) -> dict[str, Any]:
    with _handled():
        return remote.write(
            "POST",
            "config/apply",
            {"plan_id": plan["plan_id"], "allow_disruption": allow_disruption},
            revision=plan["base_revision"],
            wait=wait,
        )


@config_app.command("apply")
def config_apply(
    file: Annotated[
        Path | None,
        typer.Argument(help="Proposed YAML to write and apply. Omit to apply the file on disk."),
    ] = None,
    allow_disruption: DisruptionOption = False,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    wait: WaitOption = True,
    as_json: JsonOption = False,
) -> None:
    """Preview, then apply a full config (sync/replay changes need this)."""
    remote = connect(config, endpoint, token_file, ca_file)
    plan = _make_plan(remote, file)
    if not as_json:
        print_plan(plan)
    if (plan.get("plan") or plan).get("disruptive") and not allow_disruption:
        raise _fail("this change restarts every stream; rerun with --allow-disruption", 2)
    op = _apply_plan(remote, plan, allow_disruption, wait)
    _emit(op) if as_json else _print_operation(remote, op, "applied")


@config_app.command("reload")
def config_reload(
    apply: Annotated[bool, typer.Option("--apply", help="Apply the previewed changes.")] = False,
    allow_disruption: DisruptionOption = False,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    wait: WaitOption = True,
    as_json: JsonOption = False,
) -> None:
    """Preview converging to the YAML on disk; --apply makes it so."""
    remote = connect(config, endpoint, token_file, ca_file)
    with _handled():
        plan = remote.client.request("POST", "config/reload", {"preview": True})
    if not apply:
        _emit(plan) if as_json else print_plan(plan)
        return
    if not as_json:
        print_plan(plan)
    if (plan.get("plan") or plan).get("disruptive") and not allow_disruption:
        raise _fail("this change restarts every stream; rerun with --allow-disruption", 2)
    op = _apply_plan(remote, plan, allow_disruption, wait)
    _emit(op) if as_json else _print_operation(remote, op, "reloaded")


# ---------------------------------------------------------------------------
# events / logs
# ---------------------------------------------------------------------------


def events(
    last_event_id: Annotated[
        str | None, typer.Option("--since", help="Resume after this event id.")
    ] = None,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    as_json: JsonOption = False,
) -> None:
    """Follow engine and camera events until interrupted."""
    remote = connect(config, endpoint, token_file, ca_file)
    try:
        for event in remote.client.events(last_event_id):
            if as_json:
                print(
                    json.dumps({"id": event.id, "event": event.event, "data": event.data}),
                    flush=True,
                )
            else:
                console.print(f"{event.id} [bold]{event.event}[/] {json.dumps(event.data)}")
    except ControlClientError as exc:
        raise _client_failure(exc) from exc
    except KeyboardInterrupt:
        pass


def logs(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=2000)] = 200,
    config: ConfigOption = None,
    endpoint: EndpointOption = None,
    token_file: TokenFileOption = None,
    ca_file: CaFileOption = None,
    as_json: JsonOption = False,
) -> None:
    """Print recent log records from the running instance."""
    remote = connect(config, endpoint, token_file, ca_file)
    with _handled():
        records = remote.get(f"logs?limit={limit}")["records"]
    if as_json:
        _emit(records)
        return
    for record in records:
        stamp = time.strftime("%H:%M:%S", time.localtime(float(record.get("time") or 0)))
        print(
            f"{stamp} {record.get('level', ''):<7} "
            f"{record.get('logger', '')}: {record.get('message', '')}"
        )


def register(app: typer.Typer) -> None:
    app.command("status")(status)
    app.command("events")(events)
    app.command("logs")(logs)
    app.add_typer(camera_app, name="camera")
    app.add_typer(engine_app, name="engine")
    app.add_typer(config_app, name="config")
