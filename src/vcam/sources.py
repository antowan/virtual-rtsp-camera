"""File and live input handling shared by models, FFmpeg and ffprobe."""

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

LIVE_READ_TIMEOUT = 5.0


def is_live_source(source: Path | str) -> bool:
    return isinstance(source, str) and source.startswith(("rtsp://", "udp://"))


def parse_source(value: Path | str) -> Path | str:
    if not isinstance(value, (Path, str)):
        raise ValueError("source must be a file path or live URL")
    if isinstance(value, str) and "://" in value:
        if not is_live_source(value):
            raise ValueError("live sources must use rtsp:// or udp://")
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.port == 0:
            raise ValueError("live source URL requires a host and a valid port")
        if parsed.scheme == "udp" and parsed.port is None:
            raise ValueError("UDP source URL requires a port")
        return value
    try:
        return Path(value).expanduser()
    except RuntimeError as exc:
        raise ValueError("cannot resolve the home directory in the source path") from exc


def live_input_options(source: Path | str, timeout: float = LIVE_READ_TIMEOUT) -> list[str]:
    if not is_live_source(source):
        return []
    options = ["-rtsp_transport", "tcp"] if str(source).startswith("rtsp://") else []
    return [*options, "-fflags", "nobuffer", "-timeout", str(int(timeout * 1_000_000))]


def display_source(source: Path | str) -> str:
    """Keep URL credentials out of logs and health snapshots."""
    if not is_live_source(source):
        return str(source)
    parsed = urlsplit(str(source))
    return urlunsplit(
        parsed._replace(
            netloc=parsed.netloc.rsplit("@", 1)[-1],
            # Source-specific query fields can carry arbitrary secret names.
            query="[REDACTED]" if parsed.query else "",
            fragment="",
        )
    )


_LOG_URL = re.compile(r"""(?:rtsp|udp)://[^\s'"<>]+""")
_LOG_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+")


def redact_text(text: str) -> str:
    """Sanitize free-form child output before retaining or returning it."""

    def safe_url(match: re.Match[str]) -> str:
        try:
            return display_source(match.group())
        except ValueError:
            return "[REDACTED]"

    return _LOG_AUTH.sub(r"\1 [REDACTED]", _LOG_URL.sub(safe_url, text))
