"""Shared pytest fixtures."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from vcam.models import CameraSpec, CameraStack
from vcam.probe import MediaInfo


@pytest.fixture(autouse=True)
def isolate_control_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests and their CLI subprocesses away from the operator's state and endpoints."""
    monkeypatch.setenv("VCAM_STATE_DIR", str(tmp_path / "state"))
    for name in ("VCAM_ENDPOINT", "VCAM_CONTROL_ENDPOINT", "VCAM_CONTROL_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_thread_leaks() -> object:
    """Fail a test that leaves one of our threads running.

    Servers and players are daemon threads, so a lifecycle bug is invisible to
    the interpreter and to every assertion about bytes on the wire. It only
    shows up as a thread that outlives the test that started it.
    """
    before = {thread.ident for thread in threading.enumerate()}
    yield

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        leaked = [
            thread
            for thread in threading.enumerate()
            if thread.ident not in before
            and (thread.name.startswith("vcam-") or "process_request" in thread.name)
        ]
        if not leaked:
            return
        time.sleep(0.05)
    pytest.fail(f"threads outlived the test: {[thread.name for thread in leaked]}")


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A placeholder file that exists on disk (content is never decoded in unit tests)."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 64)
    return path


@pytest.fixture
def h264_info(video_file: Path) -> MediaInfo:
    return MediaInfo(
        path=video_file,
        codec="h264",
        width=1920,
        height=1080,
        fps=30.0,
        duration=10.0,
        pix_fmt="yuv420p",
    )


@pytest.fixture
def mpeg4_info(video_file: Path) -> MediaInfo:
    return MediaInfo(
        path=video_file,
        codec="mpeg4",
        width=640,
        height=480,
        fps=15.0,
        duration=10.0,
        pix_fmt="yuv420p",
    )


@pytest.fixture
def stack(video_file: Path) -> CameraStack:
    return CameraStack(
        cameras=[
            CameraSpec(name="cam1", source=video_file),
            CameraSpec(name="cam2", source=video_file, start_offset=5),
        ]
    )
