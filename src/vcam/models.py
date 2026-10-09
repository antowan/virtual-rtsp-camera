"""Configuration models for virtual RTSP cameras."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .sources import LIVE_READ_TIMEOUT, is_live_source, parse_source

CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RESOLUTION_RE = re.compile(r"^(\d+)x(\d+)$")
BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")
#: MediaMTX only accepts this character set in plain credentials.
CREDENTIAL_RE = re.compile(r"^[a-zA-Z0-9!$()*+.;<=>\[\]^_\-{}@#&]+$")
CREDENTIAL_CHARS = "a-z A-Z 0-9 ! $ ( ) * + . ; < = > [ ] ^ _ - { } @ # &"


class StreamMode(str, Enum):
    """How the source video is turned into an RTSP stream."""

    AUTO = "auto"
    COPY = "copy"
    TRANSCODE = "transcode"


class Transport(str, Enum):
    TCP = "tcp"
    UDP = "udp"


class VideoCodec(str, Enum):
    H264 = "h264"
    H265 = "h265"


class SimulationMode(str, Enum):
    """Camera fault to simulate on top of the normal feed.

    ``normal`` leaves the feed untouched. Every other mode but ``flaky`` is
    expressed inside the publisher's ffmpeg filter graph; ``flaky`` is the one
    fault that needs the stream itself to go away, so the supervisor drives it.
    """

    NORMAL = "normal"
    NOISE = "noise"
    DEGRADED = "degraded"
    FROZEN = "frozen"
    BLACKOUT = "blackout"
    FLAKY = "flaky"
    STUTTER = "stutter"


#: Modes whose behaviour varies over time, driven by `interval` and `duration`.
TEMPORAL_SIMULATION_MODES = frozenset({SimulationMode.FLAKY, SimulationMode.STUTTER})


ENCODER_BY_CODEC = {
    VideoCodec.H264: "libx264",
    VideoCodec.H265: "libx265",
}

#: Source codecs that can be re-published without re-encoding.
RTSP_PASSTHROUGH_CODECS = frozenset({"h264", "hevc", "h265"})


class VideoSettings(BaseModel):
    """Encoding parameters, only used when a camera actually transcodes."""

    model_config = ConfigDict(extra="forbid")

    codec: VideoCodec = VideoCodec.H264
    encoder: str | None = Field(
        default=None,
        description="Explicit ffmpeg encoder, e.g. h264_nvenc. Overrides `codec`.",
    )
    resolution: str | None = Field(default=None, description="WIDTHxHEIGHT, e.g. 1280x720")
    fps: float | None = Field(default=None, gt=0, le=240)
    bitrate: str | None = Field(default=None, description="e.g. 2M or 800k")
    gop: int | None = Field(default=None, gt=0, description="Keyframe interval in frames")
    preset: str = "veryfast"

    @field_validator("resolution")
    @classmethod
    def _check_resolution(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not RESOLUTION_RE.match(value):
            raise ValueError(f"resolution must look like 1280x720, got {value!r}")
        return value

    @field_validator("bitrate")
    @classmethod
    def _check_bitrate(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not BITRATE_RE.match(value):
            raise ValueError(f"bitrate must look like 2M or 800k, got {value!r}")
        return value

    @property
    def ffmpeg_encoder(self) -> str:
        return self.encoder or ENCODER_BY_CODEC[self.codec]

    def scale_size(self) -> tuple[int, int] | None:
        if self.resolution is None:
            return None
        match = RESOLUTION_RE.match(self.resolution)
        assert match is not None  # guaranteed by the validator
        return int(match.group(1)), int(match.group(2))


class SimulationSpec(BaseModel):
    """Fault profile applied to one camera.

    Defaults to a clean feed. The temporal modes (``flaky``, ``stutter``)
    repeat an event every ``interval`` seconds, each lasting ``duration``
    seconds.
    """

    model_config = ConfigDict(extra="forbid")

    mode: SimulationMode = SimulationMode.NORMAL
    noise_level: int = Field(
        default=30, ge=1, le=100, description="Noise amplitude for the `noise` mode"
    )
    interval: float = Field(default=30.0, gt=0, description="Seconds between flaky/stutter events")
    duration: float = Field(
        default=5.0, gt=0, description="How long each flaky/stutter event lasts"
    )
    filters: str | None = Field(
        default=None,
        description="Extra ffmpeg filters appended to the chain, e.g. 'gblur=sigma=2'",
    )

    @field_validator("filters")
    @classmethod
    def _check_filters(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Filters are passed to ffmpeg via exec argv (never a shell), so the
        # goal is to reject clearly unintended input rather than to sandbox.
        # Legitimate chains need : (params), ' (quoting), and = (key/value).
        import re

        if not re.match(r"^[a-zA-Z0-9\[\]{}(),=;:_\-.' ]+$", value):
            raise ValueError(
                f"ffmpeg filter contains unsupported characters: {value!r}. "
                f"Allowed: alphanumeric, spaces, and []{{}},=;:_-.'"
            )
        return value

    @property
    def is_temporal(self) -> bool:
        return self.mode in TEMPORAL_SIMULATION_MODES


class CameraSpec(BaseModel):
    """A single virtual camera fed by a video file or live RTSP/UDP source."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source: Path | str
    source_timeout: float = Field(default=LIVE_READ_TIMEOUT, gt=0, le=60)
    enabled: bool = True
    loop: bool = True
    realtime: bool = Field(default=True, description="Pace the file at native frame rate (-re)")
    mode: StreamMode = StreamMode.AUTO
    start_offset: float = Field(default=0.0, ge=0, description="Seek into the file, in seconds")
    transport: Transport = Transport.TCP
    audio: bool = False
    port: int | None = Field(
        default=None,
        gt=0,
        le=65535,
        description="Override the RTSP port for this camera (spawns a dedicated server)",
    )
    video: VideoSettings = Field(default_factory=VideoSettings)
    simulation: SimulationSpec = Field(default_factory=SimulationSpec)
    sync_group: str | None = Field(
        default=None, description="Opt-in shared scene clock for compatible H.264 copy cameras."
    )

    @model_validator(mode="after")
    def _check_sync_settings(self) -> CameraSpec:
        if self.is_live and self.start_offset:
            raise ValueError("start_offset is only supported for file sources")
        if self.sync_group is None:
            return self
        if self.is_live:
            raise ValueError("sync_group is file-replay only; live sources cannot join it")
        if not CAMERA_NAME_RE.fullmatch(self.sync_group):
            raise ValueError("sync_group must be a valid nonempty group name")
        if (
            self.mode not in (StreamMode.AUTO, StreamMode.COPY)
            or not self.loop
            or not self.realtime
            or self.start_offset != 0
            or self.transport is not Transport.TCP
            or self.audio
            or self.video != VideoSettings()
            or self.simulation != SimulationSpec()
        ):
            raise ValueError(
                "synchronized cameras require looping realtime H.264 copy over TCP, "
                "zero start_offset, no audio, and default video/simulation settings"
            )
        return self

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not CAMERA_NAME_RE.match(value):
            raise ValueError(
                f"camera name {value!r} is not a valid RTSP path segment "
                "(use letters, digits, '_', '-', '.')"
            )
        return value

    @field_validator("source", mode="before")
    @classmethod
    def _expand_source(cls, value: Path | str) -> Path | str:
        return parse_source(value)

    @property
    def is_live(self) -> bool:
        return is_live_source(self.source)

    def path_suffix(self) -> str:
        return self.name


class ReplaySpec(BaseModel):
    """A capture file replayed over its own RTSP listener.

    Replay does not go through MediaMTX — a media server re-packetises RTP,
    which would erase exactly the wire-level detail a capture exists to
    preserve — so each replay owns a dedicated port rather than sharing the
    camera server's.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    source: Path = Field(description="Path to a .pcap or .pcapng capture")
    port: int = Field(gt=0, le=65535, description="RTSP port for this replay's own listener")
    enabled: bool = True
    loop: bool = True
    speed: float = Field(default=1.0, gt=0, le=100, description="Playback speed multiplier")
    rewrite_on_loop: bool = Field(
        default=True,
        description=(
            "Keep RTP sequence numbers and timestamps monotonic across loops. "
            "Disable only to reproduce what a raw rewind looks like to a decoder."
        ),
    )
    sdp: Path | None = Field(
        default=None,
        description="Override the session description when the capture has no DESCRIBE",
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not CAMERA_NAME_RE.match(value):
            raise ValueError(
                f"replay name {value!r} is not a valid RTSP path segment "
                "(use letters, digits, '_', '-', '.')"
            )
        return value

    @field_validator("source", "sdp")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return Path(value).expanduser() if value is not None else None

    def path_suffix(self) -> str:
        return self.name


class AuthSpec(BaseModel):
    """Credentials required from RTSP readers."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("username", "password")
    @classmethod
    def _check_credential(cls, value: str) -> str:
        if not CREDENTIAL_RE.match(value):
            raise ValueError(
                "credentials may only contain the characters accepted by MediaMTX: "
                f"{CREDENTIAL_CHARS}"
            )
        return value


class ServerSpec(BaseModel):
    """MediaMTX server settings shared by all cameras."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="0.0.0.0", description="Bind address for the RTSP listener")
    rtsp_port: int = Field(default=8554, gt=0, le=65535)
    api_port: int = Field(default=9997, gt=0, le=65535)
    rtp_port: int = Field(
        default=8000,
        gt=0,
        le=65500,
        description="Base of the UDP RTP/RTCP port block (8 ports per server instance)",
    )
    log_level: str = "warn"
    read_timeout: str = "10s"
    write_timeout: str = "10s"
    auth: AuthSpec | None = None
    ntp_server: str | None = Field(
        default=None,
        description=(
            "Adjust the visible system clock from this NTP server before starting. "
            "Requires a container with cap_add: [SYS_TIME]; safe only in an isolated VM "
            "unless changing the native Linux host clock is explicitly intended."
        ),
    )

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        allowed = {"error", "warn", "info", "debug"}
        if value not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return value


class IngestSpec(BaseModel):
    """Optional, separately supervised simulator RTSP/TCP ingest."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    rtsp_port: int = Field(default=8654, gt=0, le=65535)
    password: str = ""

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return AuthSpec._check_credential(value) if value else value

    @model_validator(mode="after")
    def _require_password_for_remote_host(self) -> IngestSpec:
        if self.host not in {"127.0.0.1", "::1", "localhost"} and not self.password:
            raise ValueError("ingest.password is required when ingest.host is not loopback")
        return self


class CameraStack(BaseModel):
    """Top-level configuration: one server definition, its cameras and replays."""

    model_config = ConfigDict(extra="forbid")

    server: ServerSpec = Field(default_factory=ServerSpec)
    ingest: IngestSpec | None = None
    cameras: list[CameraSpec] = Field(default_factory=list)
    replays: list[ReplaySpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_paths(self) -> CameraStack:
        groups: dict[str, list[CameraSpec]] = {}
        for camera in self.enabled_cameras:
            if camera.sync_group is not None:
                groups.setdefault(camera.sync_group, []).append(camera)
        for name, members in groups.items():
            if len(members) < 2:
                raise ValueError(f"sync_group {name!r} requires at least two enabled cameras")
            for member in members:
                if sum(camera.name == member.name for camera in self.enabled_cameras) != 1:
                    raise ValueError("synchronized camera names must be unique across all ports")
        seen: set[tuple[int, str]] = set()
        for camera in self.cameras:
            key = (camera.port or self.server.rtsp_port, camera.name)
            if key in seen:
                raise ValueError(
                    f"duplicate camera path: rtsp://...:{key[0]}/{key[1]} is declared twice"
                )
            seen.add(key)

        # A replay runs its own listener, so it cannot share a port with the
        # MediaMTX instance serving the cameras — not even on a different path.
        camera_ports = {camera.port or self.server.rtsp_port for camera in self.cameras}
        replay_ports: set[int] = set()
        for replay in self.replays:
            if replay.port in camera_ports:
                raise ValueError(
                    f"replay {replay.name!r} uses port {replay.port}, which is already "
                    "served by MediaMTX; give the replay its own port"
                )
            if replay.port in replay_ports:
                raise ValueError(f"port {replay.port} is claimed by two replays")
            replay_ports.add(replay.port)
        if self.ingest is not None and self.ingest.rtsp_port in camera_ports | replay_ports:
            raise ValueError("ingest RTSP port must not overlap camera or replay ports")
        return self

    @property
    def enabled_cameras(self) -> list[CameraSpec]:
        return [camera for camera in self.cameras if camera.enabled]

    @property
    def enabled_replays(self) -> list[ReplaySpec]:
        return [replay for replay in self.replays if replay.enabled]

    def effective_port(self, camera: CameraSpec) -> int:
        """RTSP port actually used by a camera (its override or the server's default)."""
        return camera.port or self.server.rtsp_port

    def publish_url(self, camera: CameraSpec) -> str:
        """Internal URL the local ffmpeg publisher pushes to (loopback, no authentication).

        This is used only for local inter-process communication between ffmpeg and
        MediaMTX; external clients should use read_url.
        """
        return f"rtsp://127.0.0.1:{self.effective_port(camera)}/{camera.path_suffix()}"

    def replay_url(
        self,
        replay: ReplaySpec,
        host: str | None = None,
        *,
        with_credentials: bool = True,
    ) -> str:
        """Public URL for reading a replayed capture from a client.

        The replay runs its own RTSP server on its own port (not the shared
        MediaMTX instance).
        """
        display_host = host or self.display_host()
        credentials = ""
        if with_credentials and self.server.auth is not None:
            credentials = (
                f"{quote(self.server.auth.username, safe='')}:"
                f"{quote(self.server.auth.password, safe='')}@"
            )
        return f"rtsp://{credentials}{display_host}:{replay.port}/{replay.path_suffix()}"

    def read_url(
        self,
        camera: CameraSpec,
        host: str | None = None,
        *,
        with_credentials: bool = True,
    ) -> str:
        """Public URL a client uses to read a camera's RTSP stream.

        The camera is served from the shared MediaMTX server instance on either
        its configured port or the server's default port.
        """
        display_host = host or self.display_host()
        credentials = ""
        if with_credentials and self.server.auth is not None:
            credentials = (
                f"{quote(self.server.auth.username, safe='')}:"
                f"{quote(self.server.auth.password, safe='')}@"
            )
        port = self.effective_port(camera)
        return f"rtsp://{credentials}{display_host}:{port}/{camera.path_suffix()}"

    def display_host(self) -> str:
        """Hostname to use in URLs (127.0.0.1 if server is bound to all interfaces)."""
        if self.server.host in ("0.0.0.0", "::", ""):
            return "127.0.0.1"
        return self.server.host
