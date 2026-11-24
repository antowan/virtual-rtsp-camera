"""CLI command builders and helpers for vcam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import (
    CameraStack,
    SimulationMode,
    SimulationSpec,
    StreamMode,
    Transport,
    VideoCodec,
)


@dataclass
class OverridesResolver:
    """Turn CLI arguments into validated config patches.

    This class encapsulates the logic of combining user-provided CLI overrides
    into nested model structures, making it testable in isolation and reusable
    by different CLI commands.
    """

    def video_overrides(
        self,
        resolution: str | None,
        fps: float | None,
        bitrate: str | None,
        codec: VideoCodec | None,
        encoder: str | None,
        gop: int | None,
        preset: str | None,
    ) -> dict[str, object]:
        """Build a VideoSettings override dict from CLI arguments."""
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

    def simulation_overrides(
        self,
        simulation: SimulationMode | None,
        noise_level: int | None,
        interval: float | None,
        duration: float | None,
        filters: str | None,
    ) -> dict[str, object]:
        """Build a SimulationSpec override dict from CLI arguments."""
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

    def apply_overrides(
        self,
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
        """Apply CLI overrides to a stack in-place.

        Args:
            stack: The CameraStack to patch.
            host: Override server host.
            port: Override server RTSP port.
            api_port: Override server API port.
            log_level: Override server log level.
            username: Auth username (requires password).
            password: Auth password (requires username).
            ntp_server: Override NTP server for clock sync.
            mode: Override all cameras' stream mode.
            loop: Override all cameras' loop setting.
            realtime: Override all cameras' realtime setting.
            start_offset: Override all cameras' start offset.
            transport: Override all cameras' transport.
            audio: Override all cameras' audio setting.
            video: Video setting overrides (applied to all cameras).
            simulation: Simulation spec overrides (applied to all cameras).
        """
        server = stack.server
        if host is not None:
            server.host = host
        if port is not None:
            server.rtsp_port = port
        if api_port is not None:
            server.api_port = api_port
        if log_level is not None:
            server.log_level = log_level
        if ntp_server is not None:
            server.ntp_server = ntp_server
        if username or password:
            if not (username and password):
                raise ValueError("--username and --password must be provided together")
            from .models import AuthSpec

            server.auth = AuthSpec(username=username, password=password)

        for camera in stack.cameras:
            if mode is not None:
                camera.mode = mode
            if loop is not None:
                camera.loop = loop
            if realtime is not None:
                camera.realtime = realtime
            if start_offset is not None:
                camera.start_offset = start_offset
            if transport is not None:
                camera.transport = transport
            if audio is not None:
                camera.audio = audio
            if video:
                camera.video = camera.video.model_copy(update=video)
            if simulation:
                camera.simulation = SimulationSpec.model_validate(
                    camera.simulation.model_dump() | simulation
                )


@dataclass
class RunContext:
    """Dependencies for constructing and running a Supervisor.

    This dataclass encapsulates the context needed to launch an RTSP server,
    making it easier to reuse supervisor initialization logic across CLI
    commands, API endpoints, or tests without passing 7+ individual parameters.

    Attributes:
        stack: The CameraStack defining all cameras to publish.
        mediamtx_binary: Path to the MediaMTX binary; None when no MediaMTX
            instance is needed (a replay-only stack).
        work_dir: Working directory for generated configs.
        ffmpeg_log_level: Log level for ffmpeg (e.g. "warning", "info").
        health_file: Optional path to write JSON health snapshots every 5s.
        verify: Whether to wait for all paths to start publishing.
        max_restarts: Max restarts per process before giving up.
        verbose: Enable debug logging.
    """

    stack: CameraStack
    mediamtx_binary: Path | None
    work_dir: Path
    ffmpeg_log_level: str = "warning"
    health_file: Path | None = None
    verify: bool = True
    max_restarts: int | None = None
    verbose: bool = False
