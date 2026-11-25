"""Tests for the process supervisor's planning, backoff and health reporting."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from vcam.models import AuthSpec, CameraSpec, CameraStack, StreamMode
from vcam.supervisor import BACKOFF_MAX, ManagedProcess, Supervisor, SupervisorError


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "mediamtx"
    binary.write_text("#!/bin/sh\nsleep 60\n")
    binary.chmod(0o755)
    return binary


def make_supervisor(stack: CameraStack, binary: Path, tmp_path: Path) -> Supervisor:
    return Supervisor(stack, binary, work_dir=tmp_path / "work", verify=False)


# ---------------------------------------------------------------------------
# prepare()
# ---------------------------------------------------------------------------


def test_prepare_builds_one_process_per_camera(
    stack: CameraStack, fake_binary: Path, tmp_path: Path
) -> None:
    supervisor = make_supervisor(stack, fake_binary, tmp_path)
    supervisor.prepare()

    assert len(supervisor.servers) == 1
    assert [runtime.camera.name for runtime in supervisor.runtimes] == ["cam1", "cam2"]
    assert supervisor.servers[0].command[0] == str(fake_binary)


def test_prepare_writes_a_config_per_instance(
    video_file: Path, fake_binary: Path, tmp_path: Path
) -> None:
    stack = CameraStack(
        cameras=[
            CameraSpec(name="a", source=video_file),
            CameraSpec(name="b", source=video_file, port=8600),
        ]
    )
    supervisor = make_supervisor(stack, fake_binary, tmp_path)
    supervisor.prepare()

    written = sorted(path.name for path in (tmp_path / "work").iterdir())
    assert written == ["mediamtx-8554.yml", "mediamtx-8600.yml"]
    assert len(supervisor.servers) == 2


def test_prepare_rejects_missing_sources(fake_binary: Path, tmp_path: Path) -> None:
    stack = CameraStack(cameras=[CameraSpec(name="a", source=tmp_path / "nope.mp4")])
    supervisor = make_supervisor(stack, fake_binary, tmp_path)

    with pytest.raises(SupervisorError, match="not found"):
        supervisor.prepare()


def test_prepare_rejects_empty_stack(video_file: Path, fake_binary: Path, tmp_path: Path) -> None:
    stack = CameraStack(cameras=[CameraSpec(name="a", source=video_file, enabled=False)])
    supervisor = make_supervisor(stack, fake_binary, tmp_path)

    with pytest.raises(SupervisorError, match="no enabled cameras"):
        supervisor.prepare()


def test_prepare_requires_mediamtx_to_serve_cameras(stack: CameraStack, tmp_path: Path) -> None:
    supervisor = Supervisor(stack, None, work_dir=tmp_path / "work", verify=False)

    with pytest.raises(SupervisorError, match="MediaMTX binary is required"):
        supervisor.prepare()


def test_publishers_target_loopback_not_the_bind_host(
    stack: CameraStack, fake_binary: Path, tmp_path: Path
) -> None:
    stack.server.host = "10.0.0.5"
    supervisor = make_supervisor(stack, fake_binary, tmp_path)
    supervisor.prepare()

    for runtime in supervisor.runtimes:
        assert runtime.process.command[-1].startswith("rtsp://127.0.0.1:8554/")


def test_credentials_stay_out_of_the_reported_url(
    stack: CameraStack, fake_binary: Path, tmp_path: Path
) -> None:
    stack.server.auth = AuthSpec(username="reader", password="s3cr3t")
    supervisor = make_supervisor(stack, fake_binary, tmp_path)
    supervisor.prepare()

    runtime = supervisor.runtimes[0]
    assert "s3cr3t" not in runtime.read_url
    assert "s3cr3t" in runtime.read_url_with_credentials
    assert "s3cr3t" not in " ".join(runtime.process.command)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_snapshot_has_no_credentials(
    stack: CameraStack, fake_binary: Path, tmp_path: Path
) -> None:
    stack.server.auth = AuthSpec(username="reader", password="s3cr3t")
    supervisor = make_supervisor(stack, fake_binary, tmp_path)
    supervisor.prepare()

    snapshot = supervisor.health_snapshot()
    assert "s3cr3t" not in str(snapshot)


def test_health_file_is_written(stack: CameraStack, fake_binary: Path, tmp_path: Path) -> None:
    health = tmp_path / "health" / "state.json"
    supervisor = Supervisor(
        stack, fake_binary, work_dir=tmp_path / "work", verify=False, health_file=health
    )
    supervisor.prepare()
    supervisor._write_health()

    import json

    payload = json.loads(health.read_text())
    assert [camera["name"] for camera in payload["cameras"]] == ["cam1", "cam2"]
    assert payload["cameras"][0]["mode"] in {mode.value for mode in StreamMode}
    assert payload["servers"][0]["name"] == "mediamtx:8554"


# ---------------------------------------------------------------------------
# restart backoff
# ---------------------------------------------------------------------------


def test_backoff_grows_and_is_capped() -> None:
    process = ManagedProcess(name="cam", command=["true"], kind="publisher")

    delays = []
    for _ in range(10):
        process.consecutive_failures += 1
        delays.append(process.backoff())

    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert delays == sorted(delays)
    assert max(delays) == BACKOFF_MAX


def test_stable_process_resets_the_backoff() -> None:
    process = ManagedProcess(name="cam", command=["true"], kind="publisher")
    process.consecutive_failures = 6
    process.started_at = time.monotonic() - 120  # ran happily for two minutes

    process.note_exit(1)
    assert process.consecutive_failures == 1
    assert process.backoff() == 1.0


def test_crash_loop_keeps_increasing_the_backoff() -> None:
    process = ManagedProcess(name="cam", command=["true"], kind="publisher")
    process.consecutive_failures = 2
    process.started_at = time.monotonic()  # died immediately

    process.note_exit(1)
    assert process.consecutive_failures == 3


def test_managed_process_reports_failure_to_start(tmp_path: Path) -> None:
    process = ManagedProcess(
        name="cam", command=[str(tmp_path / "does-not-exist")], kind="publisher"
    )
    assert process.start() is False
    assert process.running is False
    assert process.retry_at > 0


@pytest.mark.skipif(not hasattr(os, "getpgid"), reason="POSIX process groups")
def test_children_stay_in_vcams_process_group() -> None:
    # launchd/systemd reap the job's process group when vcam dies; a child in
    # its own session would survive and keep the RTSP/API ports bound.
    process = ManagedProcess(name="mediamtx", command=["sleep", "30"], kind="server")
    assert process.start()
    try:
        assert process.pid is not None
        assert os.getpgid(process.pid) == os.getpgrp()
    finally:
        process.stop()


# ---------------------------------------------------------------------------
# stopping during startup
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_stop_during_startup_exits_cleanly(video_file: Path, tmp_path: Path) -> None:
    binary = tmp_path / "mediamtx-never-listens"
    binary.write_text("#!/bin/sh\nexec sleep 60\n")
    binary.chmod(0o755)
    stack = CameraStack(cameras=[CameraSpec(name="a", source=video_file, port=_free_port())])
    supervisor = make_supervisor(stack, binary, tmp_path)
    supervisor.prepare()
    threading.Timer(0.3, supervisor._stop.set).start()

    started = time.monotonic()
    try:
        assert supervisor._serve() == 0
    finally:
        supervisor.shutdown()

    assert time.monotonic() - started < 5  # not the 15 s MediaMTX port timeout
    assert not any(server.running for server in supervisor.servers)


def test_stop_cuts_stream_verification_short(
    stack: CameraStack, fake_binary: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor = make_supervisor(stack, fake_binary, tmp_path)
    supervisor.prepare()
    threading.Timer(0.3, supervisor._stop.set).start()

    started = time.monotonic()
    supervisor._verify_streams(timeout=30)

    assert time.monotonic() - started < 5
    assert "not publishing yet" not in caplog.text
