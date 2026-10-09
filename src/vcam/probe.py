"""ffprobe helpers used to inspect source video files."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import ProbeError
from .sources import LIVE_READ_TIMEOUT, display_source, is_live_source, live_input_options


@dataclass(frozen=True)
class MediaInfo:
    """Subset of ffprobe output that matters for republishing."""

    path: Path | str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration: float | None = None
    bitrate: int | None = None
    pix_fmt: str | None = None
    has_audio: bool = False

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None


def _parse_rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "0"):
        return None
    if "/" in value:
        numerator, _, denominator = value.partition("/")
        try:
            den = float(denominator)
            if den == 0:
                return None
            return float(numerator) / den
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def probe(source: Path | str, ffprobe: str = "ffprobe", timeout: float = 20.0) -> MediaInfo:
    """Run ffprobe on *source* and return the parsed :class:`MediaInfo`."""
    live = is_live_source(source)
    if not live and not Path(source).is_file():
        raise ProbeError(f"source file not found: {source}")
    if shutil.which(ffprobe) is None:
        raise ProbeError(f"{ffprobe} not found on PATH")

    cmd = [
        ffprobe,
        "-v",
        "error",
        *live_input_options(source, min(timeout, LIVE_READ_TIMEOUT)),
        *(["-analyzeduration", "500000", "-probesize", "1000000"] if live else []),
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]
    label = display_source(source)
    try:
        completed = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out on {label}") from exc
    except OSError as exc:
        raise ProbeError(f"could not execute {ffprobe}: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        detail = detail.replace(str(source), label)
        raise ProbeError(f"ffprobe failed on {label}: {detail or completed.returncode}")

    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"could not parse ffprobe output for {label}") from exc

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fmt = payload.get("format") or {}

    duration = None
    for candidate in (fmt.get("duration"), (video or {}).get("duration")):
        if candidate:
            try:
                duration = float(candidate)
                break
            except (TypeError, ValueError):
                continue

    bitrate = None
    if fmt.get("bit_rate"):
        try:
            bitrate = int(fmt["bit_rate"])
        except (TypeError, ValueError):
            bitrate = None

    if video is None:
        return MediaInfo(path=source, duration=duration, bitrate=bitrate, has_audio=has_audio)

    return MediaInfo(
        path=source,
        codec=video.get("codec_name"),
        width=video.get("width"),
        height=video.get("height"),
        fps=_parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
        duration=duration,
        bitrate=bitrate,
        pix_fmt=video.get("pix_fmt"),
        has_audio=has_audio,
    )


def try_probe(source: Path | str, ffprobe: str = "ffprobe") -> MediaInfo | None:
    """Probe *source*, returning ``None`` instead of raising on failure."""
    try:
        return probe(
            source, ffprobe=ffprobe, timeout=LIVE_READ_TIMEOUT if is_live_source(source) else 20.0
        )
    except ProbeError:
        return None
