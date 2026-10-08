"""Build the ffmpeg command that publishes a camera into MediaMTX."""

from __future__ import annotations

from .models import RTSP_PASSTHROUGH_CODECS, CameraSpec, SimulationMode, StreamMode
from .probe import MediaInfo
from .sources import live_input_options

SOFTWARE_ENCODERS = frozenset({"libx264", "libx265"})

#: What `degraded` falls back to when the camera sets no bitrate/GOP of its own:
#: a bandwidth-starved feed with visible blocking and rare keyframes.
DEGRADED_BITRATE = "150k"
DEGRADED_GOP = 300
#: Noise is worst-case for an encoder — uncapped it pushes tens of Mbit/s per
#: camera. Cap it at a plausible camera bitrate unless the camera sets its own.
NOISE_BITRATE = "4M"
#: Frame rate a `frozen` camera holds its picture at.
FREEZE_FPS = 1
#: Output rate used to refill the frames `stutter` drops, when neither the
#: camera nor the probe says otherwise.
DEFAULT_STUTTER_FPS = 25.0


def resolve_mode(camera: CameraSpec, info: MediaInfo | None) -> StreamMode:
    """Turn ``auto`` into a concrete mode using the probed source codec.

    Falls back to ``copy`` when the source could not be probed: passthrough is the
    cheapest option and any incompatibility surfaces immediately as a publisher error.
    """
    if camera.mode is not StreamMode.AUTO:
        return camera.mode
    if camera.is_live:
        return StreamMode.COPY
    if info is None or info.codec is None:
        return StreamMode.COPY
    if info.codec.lower() in RTSP_PASSTHROUGH_CODECS:
        return StreamMode.COPY
    return StreamMode.TRANSCODE


def _stutter_filters(camera: CameraSpec, info: MediaInfo | None) -> list[str]:
    """Freeze the picture for `duration` out of every `interval + duration`.

    `select` drops the frames of the freeze window and `fps` refills the gap by
    repeating the last frame, so the picture holds while the stream keeps
    flowing at a constant rate. Doing it inside one filter graph — rather than
    swapping publishers — matters: swapping tears the path's stream down and
    leaves already-attached readers stalled for good.
    """
    sim = camera.simulation
    period = sim.interval + sim.duration
    rate = camera.video.fps or (info.fps if info is not None and info.fps else None)
    rate = rate or DEFAULT_STUTTER_FPS
    # Single quotes keep the expression's commas from splitting the graph.
    return [
        f"select='lt(mod(t,{_format_number(period)}),{_format_number(sim.interval)})'",
        f"fps={_format_number(rate)}",
    ]


def simulation_filters(camera: CameraSpec, info: MediaInfo | None = None) -> list[str]:
    """ffmpeg filters contributed by the camera's simulation mode.

    Extra user filters are appended last so they apply on top of the fault.
    """
    sim = camera.simulation
    filters: list[str] = []

    if sim.mode is SimulationMode.NOISE:
        filters.append(f"noise=alls={sim.noise_level}:allf=t")
    elif sim.mode is SimulationMode.BLACKOUT:
        # lutyuv (not lut) so the filter is applied to luma/chroma whatever the
        # decoded pixel format is: with RGB input the y/u/v names are plain
        # aliases for c0/c1/c2 and would tint the picture instead of blanking it.
        filters.append("lutyuv=y=0:u=128:v=128")
    elif sim.mode is SimulationMode.FROZEN:
        filters.append(f"fps={FREEZE_FPS}")
    elif sim.mode is SimulationMode.STUTTER:
        filters.extend(_stutter_filters(camera, info))

    if sim.filters:
        filters.append(sim.filters)
    return filters


def simulation_forces_transcode(camera: CameraSpec, info: MediaInfo | None = None) -> bool:
    """Whether the simulation needs a decode/encode pass.

    Filters rewrite the pixels and ``degraded`` rewrites the bitstream; neither
    survives passthrough, so both override ``copy``.
    """
    if (
        camera.is_live and camera.simulation.mode is not SimulationMode.NORMAL
    ) or camera.simulation.mode is SimulationMode.DEGRADED:
        return True
    return bool(simulation_filters(camera, info))


def effective_mode(camera: CameraSpec, info: MediaInfo | None) -> StreamMode:
    """The mode the publisher really runs, simulation included.

    ``resolve_mode`` answers "what does this source need?"; this answers "what
    will ffmpeg actually do?", which is what the CLI and health file report.
    """
    mode = resolve_mode(camera, info)
    if mode is StreamMode.COPY and simulation_forces_transcode(camera, info):
        return StreamMode.TRANSCODE
    return mode


def build_publish_command(
    camera: CameraSpec,
    target_url: str,
    *,
    info: MediaInfo | None = None,
    ffmpeg: str = "ffmpeg",
    log_level: str = "warning",
) -> list[str]:
    """Return the full ffmpeg argv publishing *camera* to *target_url*."""
    mode = effective_mode(camera, info)

    cmd: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", log_level]

    # --- input options (order matters: these must precede -i) -----------------
    if camera.is_live:
        cmd += live_input_options(camera.source, camera.source_timeout)
    elif camera.loop:
        cmd += ["-stream_loop", "-1"]
    if not camera.is_live and camera.realtime:
        cmd += ["-re"]
        if camera.start_offset:
            # The seek below is an *output* option, so ffmpeg has to read and
            # discard the skipped head. Burst through it instead of letting -re
            # pace it in real time, which would delay the stream by start_offset.
            cmd += ["-readrate_initial_burst", _format_seconds(camera.start_offset + 1)]
    if not camera.is_live:
        cmd += ["-fflags", "+genpts"]
    cmd += ["-i", str(camera.source)]

    # Seeking on the output rather than the input. An input -ss combined with
    # -stream_loop restarts timestamps at the seek point on every loop, which
    # makes DTS run backwards and floods the log with "Non-monotonic DTS".
    # Seeking on the output skips the head exactly once, so later loops replay
    # the whole file and the feed simply stays phase-shifted from its peers.
    if camera.start_offset:
        cmd += ["-ss", _format_seconds(camera.start_offset)]

    # --- video ----------------------------------------------------------------
    if mode is StreamMode.COPY:
        cmd += ["-c:v", "copy"]
    else:
        filters = _build_filters(camera, info)
        if filters:
            cmd += ["-vf", filters]
        encoder = camera.video.ffmpeg_encoder
        cmd += ["-c:v", encoder]
        if encoder in SOFTWARE_ENCODERS:
            cmd += ["-preset", camera.video.preset, "-tune", "zerolatency"]
        bitrate = _effective_bitrate(camera)
        if bitrate:
            cmd += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", _double_bitrate(bitrate)]
        gop = _effective_gop(camera)
        if gop:
            gop_value = str(gop)
            cmd += ["-g", gop_value, "-keyint_min", gop_value, "-sc_threshold", "0"]
        cmd += ["-pix_fmt", "yuv420p"]

    # --- audio ----------------------------------------------------------------
    if camera.audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    else:
        cmd += ["-an"]

    # --- output ---------------------------------------------------------------
    cmd += ["-f", "rtsp", "-rtsp_transport", camera.transport.value, target_url]
    return cmd


def _build_filters(camera: CameraSpec, info: MediaInfo | None = None) -> str:
    filters: list[str] = []
    size = camera.video.scale_size()
    if size is not None:
        filters.append(f"scale={size[0]}:{size[1]}")
    if camera.video.fps is not None:
        filters.append(f"fps={_format_number(camera.video.fps)}")
    filters.extend(simulation_filters(camera, info))
    return ",".join(filters)


def _effective_bitrate(camera: CameraSpec) -> str | None:
    """Explicit bitrate wins; a simulation only fills in a sane default."""
    if camera.video.bitrate:
        return camera.video.bitrate
    if camera.simulation.mode is SimulationMode.DEGRADED:
        return DEGRADED_BITRATE
    if camera.simulation.mode is SimulationMode.NOISE:
        return NOISE_BITRATE
    return None


def _effective_gop(camera: CameraSpec) -> int | None:
    if camera.video.gop:
        return camera.video.gop
    if camera.simulation.mode is SimulationMode.FROZEN:
        # At 1 fps the encoder's default GOP would put minutes between
        # keyframes, so a reader would stare at nothing before its first
        # picture. A static frame costs almost nothing to send as a keyframe.
        return FREEZE_FPS
    if camera.simulation.mode is SimulationMode.DEGRADED:
        return DEGRADED_GOP
    return None


def _double_bitrate(bitrate: str) -> str:
    """Buffer size = 2x target bitrate, preserving the k/M suffix."""
    suffix = ""
    value = bitrate
    if bitrate and bitrate[-1] in "kKmM":
        suffix = bitrate[-1]
        value = bitrate[:-1]
    try:
        doubled = float(value) * 2
    except ValueError:
        return bitrate
    return f"{_format_number(doubled)}{suffix}"


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _format_seconds(value: float) -> str:
    return _format_number(value)
