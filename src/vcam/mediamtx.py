"""MediaMTX configuration rendering and server instance planning."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import CameraSpec, CameraStack, ServerSpec

LOCALHOST_IPS = ["127.0.0.1", "::1"]

#: The address MediaMTX's HTTP API binds. The port preflight has to probe this
#: same address, or it will hand out a port the API cannot actually open.
API_HOST = "127.0.0.1"


#: Number of consecutive UDP ports MediaMTX reserves for RTP/RTCP/SRTP/multicast.
UDP_BLOCK_SIZE = 8


@dataclass
class ServerInstance:
    """One MediaMTX process serving a group of cameras on a single RTSP port."""

    rtsp_port: int
    api_port: int
    rtp_port: int = 8000
    cameras: list[CameraSpec] = field(default_factory=list)
    config_path: Path | None = None
    ingest: bool = False

    @property
    def label(self) -> str:
        return f"mediamtx:{self.rtsp_port}"

    @property
    def api_url(self) -> str:
        return f"http://{API_HOST}:{self.api_port}"


def plan_instances(stack: CameraStack) -> list[ServerInstance]:
    """Group enabled cameras by effective RTSP port, one instance per port.

    Every instance needs its own HTTP API port; those are allocated from
    ``server.api_port`` upwards, skipping ports already in use.
    """
    groups: dict[int, list[CameraSpec]] = {}
    for camera in stack.enabled_cameras:
        groups.setdefault(stack.effective_port(camera), []).append(camera)

    instances: list[ServerInstance] = []
    taken: set[int] = set(groups) | {replay.port for replay in stack.enabled_replays}
    if stack.ingest is not None:
        taken.add(stack.ingest.rtsp_port)
    next_api = stack.server.api_port
    next_rtp = stack.server.rtp_port
    ports = sorted(groups)
    if stack.ingest is not None:
        ports.append(stack.ingest.rtsp_port)
    for rtsp_port in ports:
        # The API always listens on loopback (see `apiAddress` below), so the
        # port has to be probed there rather than on the wildcard address.
        api_port = _next_free_port(next_api, taken, host=API_HOST)
        taken.add(api_port)
        next_api = api_port + 1

        # Each instance needs its own RTP/RTCP/SRTP/multicast UDP block, otherwise
        # a second MediaMTX fails with "address already in use" on :8000.
        rtp_port = _next_free_udp_block(next_rtp, taken)
        taken.update(range(rtp_port, rtp_port + UDP_BLOCK_SIZE))
        next_rtp = rtp_port + UDP_BLOCK_SIZE

        instances.append(
            ServerInstance(
                rtsp_port=rtsp_port,
                api_port=api_port,
                rtp_port=rtp_port,
                cameras=groups.get(rtsp_port, []),
                ingest=stack.ingest is not None and rtsp_port == stack.ingest.rtsp_port,
            )
        )
    return instances


def _next_free_port(start: int, taken: set[int], host: str = "") -> int:
    port = start
    while port < 65535:
        if port not in taken and _port_is_free(port, host=host):
            return port
        port += 1
    raise RuntimeError(f"no free TCP port available from {start}")


def _port_is_free(port: int, kind: int = socket.SOCK_STREAM, host: str = "") -> bool:
    """Is ``port`` free *on the address the server will actually bind*?

    ``host`` matters more than it looks. Probing the wildcard address while the
    server binds ``127.0.0.1`` (as ``apiAddress`` does) reports a held port as
    free on BSD and macOS, because ``SO_REUSEADDR`` permits binding
    ``0.0.0.0:p`` while another socket holds ``127.0.0.1:p``. MediaMTX then
    fails with "address already in use" for a port we just called available.
    """
    with socket.socket(socket.AF_INET, kind) as sock:
        if kind == socket.SOCK_STREAM:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _next_free_udp_block(start: int, taken: set[int]) -> int:
    """Find a base port whose UDP block is entirely free."""
    base = start
    while base + UDP_BLOCK_SIZE < 65535:
        ports = range(base, base + UDP_BLOCK_SIZE)
        if not any(port in taken for port in ports) and all(
            _port_is_free(port, socket.SOCK_DGRAM) for port in ports
        ):
            return base
        base += UDP_BLOCK_SIZE
    raise RuntimeError(f"no free UDP port block available from {start}")


def _listen_address(host: str, port: int) -> str:
    if host in ("0.0.0.0", "::", ""):
        return f":{port}"
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def build_auth_users(server: ServerSpec) -> list[dict[str, Any]]:
    """Return the ``authInternalUsers`` entries for the requested auth mode.

    Without credentials everything is anonymous. With credentials, readers must
    authenticate while anonymous publishing stays allowed from loopback only, so
    the locally spawned ffmpeg publishers never need to carry a password.
    """
    admin_user: dict[str, Any] = {
        "user": "any",
        "pass": "",
        "ips": list(LOCALHOST_IPS),
        "permissions": [{"action": "api"}, {"action": "metrics"}, {"action": "pprof"}],
    }

    if server.auth is None:
        anonymous: dict[str, Any] = {
            "user": "any",
            "pass": "",
            "ips": [],
            "permissions": [
                {"action": "publish"},
                {"action": "read"},
                {"action": "playback"},
            ],
        }
        return [anonymous, admin_user]

    local_publisher: dict[str, Any] = {
        "user": "any",
        "pass": "",
        "ips": list(LOCALHOST_IPS),
        "permissions": [{"action": "publish"}],
    }
    reader: dict[str, Any] = {
        "user": server.auth.username,
        "pass": server.auth.password,
        "ips": [],
        "permissions": [
            {"action": "publish"},
            {"action": "read"},
            {"action": "playback"},
        ],
    }
    return [local_publisher, reader, admin_user]


def render_server_config(
    instance: ServerInstance, server: ServerSpec, stack: CameraStack | None = None
) -> dict[str, Any]:
    """Build the MediaMTX configuration mapping for one server instance."""
    config = {
        "logLevel": server.log_level,
        "logDestinations": ["stdout"],
        "readTimeout": server.read_timeout,
        "writeTimeout": server.write_timeout,
        "api": True,
        "apiAddress": f"{API_HOST}:{instance.api_port}",
        "metrics": False,
        "pprof": False,
        "playback": False,
        "rtsp": True,
        "rtspTransports": ["udp", "multicast", "tcp"],
        "rtspEncryption": "no",
        "rtspAddress": _listen_address(server.host, instance.rtsp_port),
        "rtpAddress": f":{instance.rtp_port}",
        "rtcpAddress": f":{instance.rtp_port + 1}",
        "multicastRTPPort": instance.rtp_port + 2,
        "multicastRTCPPort": instance.rtp_port + 3,
        "srtpAddress": f":{instance.rtp_port + 4}",
        "srtcpAddress": f":{instance.rtp_port + 5}",
        "multicastSRTPPort": instance.rtp_port + 6,
        "multicastSRTCPPort": instance.rtp_port + 7,
        "rtmp": False,
        "hls": False,
        "webrtc": False,
        "srt": False,
        "moq": False,
        "authMethod": "internal",
        "authInternalUsers": build_auth_users(server),
        "paths": {camera.path_suffix(): {} for camera in instance.cameras},
    }
    if instance.ingest:
        assert stack is not None and stack.ingest is not None
        ingest = stack.ingest
        local = ingest.host in {"127.0.0.1", "::1", "localhost"}
        config.update(
            rtspAddress=_listen_address(ingest.host, instance.rtsp_port),
            rtspTransports=["tcp"],
            authInternalUsers=[
                {
                    "user": "sim",
                    "pass": ingest.password,
                    "ips": list(LOCALHOST_IPS) if local else [],
                    "permissions": [{"action": "publish", "path": "~^sim/.+$"}],
                },
                {
                    "user": "any",
                    "pass": "",
                    "ips": list(LOCALHOST_IPS),
                    "permissions": [
                        {"action": "read", "path": "~^sim/.+$"},
                        {"action": "api"},
                    ],
                },
            ],
            paths={"~^sim/.+$": {}},
        )
    return config


def render_server_config_yaml(
    instance: ServerInstance, server: ServerSpec, stack: CameraStack | None = None
) -> str:
    return yaml.safe_dump(
        render_server_config(instance, server, stack), sort_keys=False, default_flow_style=False
    )


def write_server_config(
    instance: ServerInstance,
    server: ServerSpec,
    directory: Path,
    stack: CameraStack | None = None,
) -> Path:
    """Write the generated config for *instance* and record its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"mediamtx-{instance.rtsp_port}.yml"
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(render_server_config_yaml(instance, server, stack), encoding="utf-8")
    instance.config_path = path
    return path
