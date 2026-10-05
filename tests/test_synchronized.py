"""Admission, configuration and application-path shared-clock playback tests."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from fractions import Fraction
from itertools import pairwise
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from pydantic import ValidationError

from vcam.config import load_stack, save_stack
from vcam.errors import SupervisorError
from vcam.models import CameraSpec, CameraStack, ServerSpec
from vcam.supervisor import ManagedProcess, Supervisor
from vcam.synchronized import SyncMember, admit_source, compatible_sources, has_idr


def sync_stack(path: Path) -> CameraStack:
    return CameraStack(
        cameras=[
            CameraSpec(name="a", source=path, sync_group="scene"),
            CameraSpec(name="b", source=path, sync_group="scene"),
        ]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "transcode"},
        {"loop": False},
        {"realtime": False},
        {"start_offset": 1},
        {"transport": "udp"},
        {"audio": True},
        {"video": {"fps": 30}},
        {"simulation": {"mode": "flaky"}},
        {"sync_group": ""},
    ],
)
def test_sync_settings_reject_unsupported_behavior(change: dict) -> None:
    with pytest.raises(ValidationError):
        CameraSpec.model_validate(
            {"name": "a", "source": "video.mp4", "sync_group": "scene"} | change
        )


def test_groups_require_two_enabled_members_and_unambiguous_names() -> None:
    with pytest.raises(ValidationError, match="two enabled"):
        CameraStack(cameras=[CameraSpec(name="a", source="a.mp4", sync_group="scene")])
    with pytest.raises(ValidationError, match="unique across all ports"):
        CameraStack(
            cameras=[
                CameraSpec(name="a", source="a.mp4", sync_group="scene"),
                CameraSpec(name="b", source="b.mp4", sync_group="scene"),
                CameraSpec(name="a", source="a.mp4", port=8600),
            ]
        )


def test_sync_config_roundtrips_and_resolves_paths(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    save_stack(sync_stack(Path("clip.mp4")), path)
    stack = load_stack(path)
    assert [camera.sync_group for camera in stack.cameras] == ["scene", "scene"]
    assert all(camera.source == tmp_path / "clip.mp4" for camera in stack.cameras)


def test_idr_detection_does_not_trust_keyframe_flags() -> None:
    assert has_idr(b"\x00\x00\x00\x02\x65\x88")
    assert not has_idr(b"\x00\x00\x00\x02\x41\x88")
    for data in (b"\x00", b"\x00\x00\x00\x05\x65", b"\x00\x00\x00\x00"):
        with pytest.raises(ValueError):
            has_idr(data)


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    from scripts.sync_prototype.media import generate_fixture

    path = tmp_path / "clip.mp4"
    generate_fixture(path, 0, 2)
    return path


def test_admission_preserves_real_packet_bytes_and_b_frame_timing(clip: Path) -> None:
    av = pytest.importorskip("av")
    source = admit_source(clip)
    assert source.rate == Fraction(30)
    assert source.frames == 120
    assert source.points == (0, 30, 60, 90)
    assert source.packets[0].dts == -2
    with av.open(str(clip)) as container:
        assert [packet.data for packet in source.packets] == [
            bytes(packet) for packet in container.demux(video=0) if packet.size
        ]


def test_admission_rejects_invalid_and_oversized_sources(clip: Path, monkeypatch) -> None:
    invalid = clip.with_name("invalid.mp4")
    invalid.write_bytes(b"not video")
    with pytest.raises(SupervisorError, match="Cannot synchronize"):
        admit_source(invalid)
    monkeypatch.setattr("vcam.synchronized.MAX_SOURCE_BYTES", 10)
    with pytest.raises(SupervisorError, match="64 MiB"):
        admit_source(clip)


def test_group_rejects_different_rates_or_scene_lengths(clip: Path) -> None:
    from dataclasses import replace

    source = admit_source(clip)
    for incompatible in (
        replace(source, rate=Fraction(25)),
        replace(source, packets=source.packets[:-1]),
    ):
        with pytest.raises(SupervisorError, match="identical rate/frame count"):
            compatible_sources([source, incompatible])


def test_admission_supports_nonprototype_scene_and_fractional_rate(tmp_path: Path) -> None:
    av = pytest.importorskip("av")
    path = tmp_path / "fractional.mp4"
    rate = Fraction(30000, 1001)
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=rate)
        stream.width, stream.height = 64, 48
        stream.pix_fmt = "yuv420p"
        stream.options = {"x264-params": "keyint=15:min-keyint=15:scenecut=0:open-gop=0:bframes=0"}
        for index in range(45):
            frame = av.VideoFrame(64, 48, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes([32 + index]) * plane.buffer_size)
            frame.pts, frame.time_base = index, 1 / rate
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    source = admit_source(path)
    assert source.rate == rate
    assert source.frames == 45
    assert source.points == (0, 15, 30)


def test_watchdog_allows_future_idr_and_enforces_restart_budget(clip: Path, monkeypatch) -> None:
    from vcam.synchronized import SyncGroup

    source = admit_source(clip)
    group = SyncGroup("scene", [("a", source, "rtsp://unused")], max_restarts=0)
    member = group.members[0]

    class FakeProcess:
        def is_alive(self) -> bool:
            return True

    member.process = FakeProcess()
    now_ns = time.monotonic_ns()
    member.started_ns = now_ns - 8_000_000_000
    member.deadline_ns = now_ns + 5_000_000_000
    member.tick(time.monotonic())
    assert not member.last_error
    member.deadline_ns = now_ns - 3_000_000_000

    def stop() -> None:
        member.process = None

    monkeypatch.setattr(member, "stop", stop)
    member.tick(time.monotonic())
    assert "watchdog" in member.last_error
    member.tick(time.monotonic() + 2)
    assert member.gave_up
    assert member.state == "failed"
    assert member.restarts == 0


def _suspend_control_receiver(commands: Connection, events: Connection) -> None:
    events.send("ready")
    os.kill(os.getpid(), signal.SIGSTOP)
    commands.recv()


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="requires POSIX process suspension")
def test_suspended_publisher_control_cannot_deadlock_stop(clip: Path) -> None:
    from vcam.synchronized import SyncGroup

    group = SyncGroup("scene", [("a", admit_source(clip), "rtsp://unused")], max_restarts=0)
    member = group.members[0]
    member.receiver, events = group.context.Pipe(duplex=False)
    commands, member.commands = group.context.Pipe(duplex=False)
    member.process = group.context.Process(
        target=_suspend_control_receiver, args=(commands, events)
    )
    member.process.start()
    commands.close()
    events.close()
    try:
        assert member.receiver.poll(5)
        assert member.receiver.recv() == "ready"
        time.sleep(0.05)
        began = time.monotonic()
        member.stop(timeout=0.1)
        assert time.monotonic() - began < 1
        assert member.process is None
        assert member.commands is None
        assert member.receiver is None
        assert member.last_exit_code in (-signal.SIGTERM, -signal.SIGKILL)
    finally:
        member.stop(timeout=0.1)


def test_supervisor_routes_grouped_and_independent_cameras(clip: Path, tmp_path: Path) -> None:
    stack = sync_stack(clip)
    stack.cameras.append(CameraSpec(name="independent", source=clip))
    supervisor = Supervisor(stack, Path("unused"), work_dir=tmp_path / "work", verify=False)
    supervisor.prepare()
    assert len(supervisor.sync_groups) == 1
    assert isinstance(supervisor.runtimes[0].process, SyncMember)
    assert isinstance(supervisor.runtimes[2].process, ManagedProcess)
    assert len(supervisor._all_processes()) == 2  # one server, one independent publisher
    health = supervisor.health_snapshot()
    assert health["sync_groups"][0]["frames"] == 120
    assert health["cameras"][0]["sync_group"] == "scene"
    supervisor.shutdown()


def test_supervisor_revalidates_mutated_settings(clip: Path, tmp_path: Path) -> None:
    stack = sync_stack(clip)
    stack.cameras[0].audio = True
    supervisor = Supervisor(stack, Path("unused"), work_dir=tmp_path / "work", verify=False)
    with pytest.raises(ValidationError, match="no audio"):
        supervisor.prepare()


def test_startup_waits_keep_active_groups_serviced(clip: Path, tmp_path: Path, monkeypatch) -> None:
    from vcam.supervisor import _wait_for_port
    from vcam.synchronized import SyncGroup

    source = admit_source(clip)
    active = SyncGroup("active", [("a", source, "rtsp://unused")], max_restarts=0)
    starting = SyncGroup("starting", [("b", source, "rtsp://unused")], max_restarts=0)
    active.epoch_ns = time.monotonic_ns()
    ticks = []
    monkeypatch.setattr(active, "tick", lambda now: ticks.append(now))
    monkeypatch.setattr(starting, "tick", lambda now: pytest.fail("released an unready group"))
    supervisor = Supervisor(sync_stack(clip), Path("unused"), work_dir=tmp_path / "work")
    supervisor.sync_groups = [active, starting]

    def unavailable(*args, **kwargs):
        raise OSError("not listening yet")

    monkeypatch.setattr("vcam.supervisor.socket.create_connection", unavailable)
    assert not _wait_for_port("127.0.0.1", 8554, 0.01, service=supervisor._tick_sync_groups)
    assert len(ticks) == 1
    ticks.clear()
    member = starting.members[0]
    monkeypatch.setattr(member, "start", lambda: None)
    monkeypatch.setattr(member, "release", lambda: None)
    monkeypatch.setattr(member, "drain", lambda: setattr(member, "ready", True))
    monkeypatch.setattr(SyncMember, "running", property(lambda self: True))
    starting.start(service=supervisor._tick_sync_groups)
    assert len(ticks) == 1
    assert starting.epoch_ns > active.epoch_ns


def test_cli_override_rejects_invalid_sync_and_dry_run_describes_backend(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from vcam.cli import app

    path = tmp_path / "cameras.yaml"
    save_stack(sync_stack(Path("a.mp4")), path)
    runner = CliRunner()
    result = runner.invoke(app, ["run", "--config", str(path), "--audio", "--dry-run"])
    assert result.exit_code != 0
    assert "no audio" in result.output
    result = runner.invoke(app, ["run", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0
    assert "synchronized H.264 copy publisher" in result.output
    assert "group=scene" in result.output


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(
    os.environ.get("VCAM_SYNC_INTEGRATION") != "1",
    reason="opt-in 44-second application-path test; requires local MediaMTX and ffmpeg",
)
@pytest.mark.parametrize("scenario", ["baseline", "exit", "stall"])
def test_vcam_run_synchronized_application_path(tmp_path: Path, scenario: str) -> None:
    av = pytest.importorskip("av")
    pytest.importorskip("numpy")
    from scripts.sync_prototype.media import generate_fixture, read_marker

    from vcam.binaries import resolve_binary

    binary = resolve_binary(allow_download=False)
    cameras = []
    for view in range(3):
        path = tmp_path / f"view{view}.mp4"
        generate_fixture(path, view, 0 if scenario == "baseline" else 2)
        cameras.append(CameraSpec(name=f"view{view}", source=path, sync_group="scene"))
    cameras.append(CameraSpec(name="independent", source=cameras[0].source))
    port = _port()
    api_port = _port()
    config = tmp_path / "cameras.yaml"
    health = tmp_path / "health.json"
    save_stack(
        CameraStack(
            server=ServerSpec(host="127.0.0.1", rtsp_port=port, api_port=api_port, rtp_port=22000),
            cameras=cameras,
        ),
        config,
    )
    observations: dict[str, list[tuple[int, float, int]]] = {}
    marker_latencies: dict[str, list[int]] = {}
    errors: list[str] = []
    stopping = threading.Event()
    reader_stops: dict[str, threading.Event] = {}
    readers: list[threading.Thread] = []

    def read(name: str, view: int, path: str) -> None:
        observations[name] = []
        marker_latencies[name] = []
        try:
            with av.open(
                f"rtsp://127.0.0.1:{port}/{path}",
                options={"rtsp_transport": "tcp", "probesize": "32", "analyzeduration": "0"},
                timeout=(3.0, 2.0),
            ) as source:
                for frame in source.decode(video=0):
                    arrival = time.monotonic_ns()
                    if stopping.is_set() or reader_stops[name].is_set():
                        return
                    actual, marker = read_marker(frame)
                    marker_done = time.monotonic_ns()
                    assert actual == view
                    if frame.pts is not None:
                        marker_latencies[name].append(marker_done - arrival)
                        observations[name].append(
                            (marker, float(frame.pts * frame.time_base), arrival)
                        )
        except Exception as exc:
            if not stopping.is_set() and not (scenario != "baseline" and name == "main1"):
                errors.append(f"{name}: {exc}")

    def launch(name: str, view: int, path: str) -> None:
        reader_stops[name] = threading.Event()
        thread = threading.Thread(target=read, args=(name, view, path), daemon=True)
        readers.append(thread)
        thread.start()

    with (tmp_path / "run.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vcam",
                "run",
                "--config",
                str(config),
                "--mediamtx-binary",
                str(binary),
                "--health-file",
                str(health),
                "--max-restarts",
                "3",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while not health.exists():
                assert process.poll() is None, (tmp_path / "run.log").read_text()
                assert time.monotonic() < deadline, "application startup timeout"
                time.sleep(0.02)
            initial = json.loads(health.read_text())
            epoch = initial["sync_groups"][0]["epoch_ns"]
            for view in range(3):
                launch(f"main{view}", view, f"view{view}")
            launch("independent", 0, "independent")
            late = reconnected = injected = False
            while time.monotonic_ns() < epoch + 44_000_000_000:
                elapsed = (time.monotonic_ns() - epoch) / 1_000_000_000
                assert process.poll() is None, (tmp_path / "run.log").read_text()
                if elapsed > 5 and not late:
                    launch("late", 0, "view0")
                    late = True
                if elapsed > 9 and not reconnected:
                    reader_stops["late"].set()
                    launch("reconnect", 0, "view0")
                    reconnected = True
                if scenario != "baseline" and elapsed > 12 and not injected:
                    snapshot = json.loads(health.read_text())
                    affected = next(c for c in snapshot["cameras"] if c["name"] == "view1")
                    os.kill(
                        affected["pid"], signal.SIGKILL if scenario == "exit" else signal.SIGSTOP
                    )
                    injected = True
                if scenario != "baseline" and injected and "recovered" not in observations:
                    snapshot = json.loads(health.read_text())
                    affected = next(c for c in snapshot["cameras"] if c["name"] == "view1")
                    if affected["generation"] == 1 and affected["state"] == "publishing":
                        launch("recovered", 1, "view1")
                time.sleep(0.02)
            snapshot = json.loads(health.read_text())
        finally:
            stopping.set()
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
            for thread in readers:
                thread.join(4)
        (tmp_path / "observations.json").write_text(
            json.dumps(
                {
                    "observations": observations,
                    "marker_latencies_ns": marker_latencies,
                    "epoch_ns": epoch,
                    "health": snapshot,
                    "errors": errors,
                }
            )
        )
        assert process.returncode == 0, (tmp_path / "run.log").read_text()
    assert not errors, errors
    assert all(not thread.is_alive() for thread in readers)
    assert len(observations["independent"]) > 600
    keyed = {}
    for name in ("main0", "main1", "main2") if scenario == "baseline" else ("main0", "main2"):
        samples = observations[name]
        assert len(samples) > 44 * 30 * 0.8
        assert sum(marker == 0 for marker, _, _ in samples) >= 10
        assert samples[-1][2] >= epoch + 43_500_000_000
        first_marker, first_pts, _ = samples[0]
        frames = {}
        for marker, pts, arrival in samples:
            frame = first_marker + round((pts - first_pts) * 30)
            assert frame % 120 == marker
            assert frame not in frames
            assert (
                -1 / 30
                <= (arrival - epoch) / 1e9
                - (frame + snapshot["sync_groups"][0]["lead_frames"]) / 30
                <= 0.5
            )
            frames[frame] = arrival
        assert all(after == before + 1 for before, after in pairwise(frames))
        keyed[name] = frames
    common = set.intersection(*(set(frames) for frames in keyed.values()))
    spreads = [
        (
            max(frames[index] for frames in keyed.values())
            - min(frames[index] for frames in keyed.values())
        )
        / 1e6
        for index in common
    ]
    assert len(common) > 1000
    assert max(spreads) <= 1000 / 30, "decoded receipt gate failed; see observations.json"
    acquisition = {}
    for name in ("late", "reconnect") + (("recovered",) if scenario != "baseline" else ()):
        samples = observations[name]
        assert len(samples) >= 30
        first_marker, first_pts, first_arrival = samples[0]
        reference = keyed["main0"]
        initial_frame = min(
            (index for index in reference if index % 120 == first_marker),
            key=lambda index: abs(reference[index] - first_arrival),
        )
        matches = []
        decoded_frames = []
        for marker, pts, arrival in samples:
            frame = initial_frame + round((pts - first_pts) * 30)
            assert frame % 120 == marker
            decoded_frames.append(frame)
            if frame in reference:
                arrivals = [
                    arrival,
                    *(values[frame] for values in keyed.values() if frame in values),
                ]
                matches.append((max(arrivals) - min(arrivals)) / 1e6)
        assert all(after == before + 1 for before, after in pairwise(decoded_frames))
        assert len(matches) >= 30
        assert max(matches) <= 1000 / 30
        assert samples[-1][2] >= epoch + (8_500_000_000 if name == "late" else 43_500_000_000)
        acquisition[name] = f"{len(matches)} matches/{max(matches):.6f} ms max spread"
    for camera in snapshot["cameras"]:
        if camera["name"] == "view1" and scenario != "baseline":
            assert camera["generation"] == 1
        else:
            assert camera["restarts"] == 0
        if camera["name"] != "independent":
            assert camera["gop_skips"] == 0
    print(
        f"MEASURED application {scenario}: healthy_matches={len(common)} "
        f"healthy_max_spread_ms={max(spreads):.6f} 10+ wraps; independent camera active; "
        f"acquisition={acquisition}; marker_work_max_ms="
        f"{max(max(values) for values in marker_latencies.values()) / 1e6:.6f}"
    )
