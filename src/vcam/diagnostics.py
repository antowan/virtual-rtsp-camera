"""Read-only backend diagnostics. No installers, shell or caller-chosen binaries."""

from __future__ import annotations

import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import binaries
from .errors import BinaryError, PcapError, ProbeError
from .ffmpeg import resolve_mode
from .models import CameraSpec
from .pcap import backend_version
from .probe import probe


def doctor(ffmpeg: str, ffprobe: str, mediamtx_binary: Path | None) -> dict[str, Any]:
    tools: dict[str, bool] = {
        "ffmpeg": shutil.which(ffmpeg) is not None,
        "ffprobe": shutil.which(ffprobe) is not None,
    }
    try:
        binaries.resolve_binary(mediamtx_binary, allow_download=False)
    except BinaryError:
        tools["mediamtx"] = False
    else:
        tools["mediamtx"] = True
    try:
        pcap: str | None = backend_version()
    except PcapError:
        pcap = None
    return {"ok": all(tools.values()), "tools": tools, "pcap_backend": pcap}


def media_probe(camera: CameraSpec, base_dir: Path, ffprobe: str) -> dict[str, Any]:
    source = camera.source
    if isinstance(source, Path):
        source = (base_dir / source).resolve()
        if not source.is_file():
            raise ProbeError("source must be a regular media file")
    try:
        info = probe(
            source, ffprobe=ffprobe, timeout=camera.source_timeout if camera.is_live else 20
        )
    except ProbeError:
        # Child stderr can include credentials and input excerpts. Never forward it to clients.
        raise ProbeError("cannot probe this media source") from None
    fields = asdict(info)
    del fields["path"]
    return {**fields, "auto_mode": resolve_mode(camera, info).value}
