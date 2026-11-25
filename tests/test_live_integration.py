"""Opt-in real FFmpeg/MediaMTX contract check; no Unity or GPU benchmark."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from vcam.binaries import resolve_binary
from vcam.models import CameraSpec, CameraStack, IngestSpec, SimulationSpec
from vcam.supervisor import Supervisor, _wait_for_port

pytestmark = pytest.mark.skipif(
    os.environ.get("VCAM_LIVE_TESTS") != "1",
    reason="set VCAM_LIVE_TESTS=1 for real FFmpeg/MediaMTX live checks",
)


def wait_until(check, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    pytest.fail("live contract condition did not become true before timeout")


def stream_format(url: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "5000000",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-of",
            "json",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=12,
        check=True,
    )
    video = json.loads(result.stdout)["streams"][0]
    keys = (
        "codec_name",
        "profile",
        "width",
        "height",
        "r_frame_rate",
        "pix_fmt",
        "has_b_frames",
        "color_range",
        "color_space",
        "color_transfer",
        "color_primaries",
    )
    return {key: video.get(key) for key in keys}


def cpu_seconds(pid: int) -> float:
    value = subprocess.check_output(["ps", "-p", str(pid), "-o", "time="], text=True).strip()
    parts = value.split(":")
    return sum(float(part) * 60**index for index, part in enumerate(reversed(parts)))


def test_copy_fault_and_publisher_outage(tmp_path: Path) -> None:
    if platform.system() not in {"Darwin", "Linux"}:
        pytest.skip("CPU measurement uses Unix ps")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.fail("ffmpeg and ffprobe are required for VCAM_LIVE_TESTS=1")
    binary = resolve_binary(allow_download=False)
    # Never displace another session's listeners.
    for port in (8554, 8654):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                pytest.skip(f"live verification port {port} is already in use")

    ingest_url = "rtsp://127.0.0.1:8654/sim/anpr-front"
    stack = CameraStack(
        ingest=IngestSpec(),
        cameras=[
            CameraSpec(name="anpr-front", source=ingest_url),
            CameraSpec(
                name="fault",
                source=ingest_url,
                simulation=SimulationSpec.model_validate({"mode": "blackout"}),
            ),
        ],
    )
    supervisor = Supervisor(
        stack,
        binary,
        work_dir=tmp_path,
        verify=False,
        health_file=tmp_path / "health.json",
        max_restarts=0,
    )
    errors = []

    def serve():
        try:
            supervisor.run()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, name="vcam-live-test")
    publisher = None
    log = (tmp_path / "publisher.log").open("w")
    encoder = "h264_videotoolbox" if platform.system() == "Darwin" else "libx264"

    def publish():
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30",
            "-c:v",
            encoder,
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-bf",
            "0",
            "-b:v",
            "8M",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
        ]
        if encoder == "libx264":
            command += ["-preset", "veryfast", "-tune", "zerolatency"]
        command += [
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            "rtsp://sim:@127.0.0.1:8654/sim/anpr-front",
        ]
        return subprocess.Popen(command, stdout=log, stderr=log)

    def stop_publisher():
        assert publisher is not None
        publisher.terminate()
        try:
            publisher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            publisher.kill()
            publisher.wait(timeout=5)

    def camera_state(name):
        return next(
            camera for camera in supervisor.health_snapshot()["cameras"] if camera["name"] == name
        )

    try:
        thread.start()
        wait_until(lambda: len(supervisor.runtimes) == 2)
        wait_until(lambda: camera_state("anpr-front")["state"] == "waiting-for-source")
        assert _wait_for_port("127.0.0.1", 8654)
        for credentials in ("", "intruder:wrong@"):
            denied = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=64x64:rate=1",
                    "-frames:v",
                    "1",
                    "-c:v",
                    "libx264",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    f"rtsp://{credentials}127.0.0.1:8654/sim/forbidden",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert denied.returncode != 0
            assert "401 Unauthorized" in denied.stderr
        publisher = publish()
        wait_until(lambda: camera_state("anpr-front")["state"] == "running", timeout=45)
        assert publisher.poll() is None, (tmp_path / "publisher.log").read_text()
        wait_until(lambda: camera_state("fault")["state"] == "running")
        ingest = stream_format(ingest_url)
        output = stream_format("rtsp://127.0.0.1:8554/anpr-front")
        assert output == ingest
        assert output["codec_name"] == "h264"
        assert output["profile"] == "High"
        assert (output["width"], output["height"], output["r_frame_rate"]) == (1920, 1080, "30/1")
        assert output["has_b_frames"] == 0
        assert output["color_space"] == "bt709"
        assert camera_state("anpr-front")["mode"] == "copy"
        assert camera_state("fault")["mode"] == "transcode"
        commands = [runtime.process.command for runtime in supervisor.runtimes]
        assert commands[0][commands[0].index("-c:v") + 1] == "copy"
        assert commands[1][commands[1].index("-c:v") + 1] == "libx264"
        pid = camera_state("anpr-front")["pid"]
        started = time.monotonic()
        cpu_start = cpu_seconds(pid)
        time.sleep(15)
        cpu = 100 * (cpu_seconds(pid) - cpu_start) / (time.monotonic() - started)
        print(f"\ncopy-forwarder CPU: {cpu:.2f}%; format: {json.dumps(output, sort_keys=True)}")
        assert cpu < 5, f"copy-forwarder CPU {cpu:.2f}% exceeds the contract's 5% limit"

        stop_publisher()
        publisher = None
        wait_until(lambda: camera_state("anpr-front")["state"] == "waiting-for-source")
        assert not supervisor.runtimes[0].process.gave_up
        publisher = publish()
        wait_until(lambda: camera_state("anpr-front")["state"] == "running", timeout=45)
        assert stream_format("rtsp://127.0.0.1:8554/anpr-front") == ingest
        assert camera_state("anpr-front")["restarts"] >= 1
    finally:
        if publisher is not None:
            stop_publisher()
        supervisor._stop.set()
        thread.join(timeout=20)
        log.close()
    assert not thread.is_alive()
    assert not errors, errors


def test_udp_source_read_timeout_and_forwarding(tmp_path: Path) -> None:
    binary = resolve_binary(allow_download=False)
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        udp_port = sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        rtsp_port = sock.getsockname()[1]
    source = f"udp://127.0.0.1:{udp_port}"
    supervisor = Supervisor(
        CameraStack(
            cameras=[
                CameraSpec(name="udp", source=source, source_timeout=2, port=rtsp_port),
            ]
        ),
        binary,
        work_dir=tmp_path,
        verify=False,
    )
    errors = []

    def serve():
        try:
            supervisor.run()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, name="vcam-live-udp-test")
    with (tmp_path / "udp-publisher.log").open("w") as log:
        publisher = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x240:rate=30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "zerolatency",
                "-g",
                "30",
                "-bf",
                "0",
                "-f",
                "mpegts",
                source,
            ],
            stdout=log,
            stderr=log,
        )
        try:
            thread.start()

            def state():
                cameras = supervisor.health_snapshot()["cameras"]
                return cameras[0]["state"] if cameras else None

            wait_until(lambda: state() == "running")
            video = stream_format(f"rtsp://127.0.0.1:{rtsp_port}/udp")
            assert (video["width"], video["height"], video["r_frame_rate"]) == (320, 240, "30/1")
            assert supervisor.health_snapshot()["cameras"][0]["mode"] == "copy"
            publisher.terminate()
            publisher.wait(timeout=5)
            wait_until(lambda: state() == "waiting-for-source", timeout=15)
        finally:
            if publisher.poll() is None:
                publisher.terminate()
                publisher.wait(timeout=5)
            supervisor._stop.set()
            thread.join(timeout=15)
    assert not thread.is_alive()
    assert not errors, errors


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _progress_frames(path: Path) -> int:
    frames = [
        int(line.split("=", 1)[1])
        for line in path.read_text().splitlines()
        if line.startswith("frame=") and line.split("=", 1)[1].isdigit()
    ]
    return frames[-1] if frames else 0


def test_control_cli_adds_camera_while_running_without_disturbing_readers(
    tmp_path: Path,
) -> None:
    """`vcam camera add` against `vcam run --control` must not touch cam1's reader."""
    import random
    import signal
    import sys

    if not shutil.which("ffmpeg"):
        pytest.fail("ffmpeg is required for VCAM_LIVE_TESTS=1")
    resolve_binary(allow_download=False)
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
         "-t", "6", "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
        timeout=60,
    )  # fmt: skip
    rtsp, api, control = _free_port(), _free_port(), _free_port()
    rtp = random.randrange(20000, 40000, 8)
    config = tmp_path / "cameras.yaml"
    config.write_text(
        "# live control e2e\n"
        f"server:\n  host: 127.0.0.1\n  rtsp_port: {rtsp}\n  api_port: {api}\n"
        f"  rtp_port: {rtp}\n"
        "cameras:\n  - name: cam1  # keep me\n    source: clip.mp4\n"
    )
    env = {**os.environ, "VCAM_STATE_DIR": str(tmp_path / "state")}
    vcam = [sys.executable, "-m", "vcam"]

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*vcam, *args, "-c", str(config)],
            env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path,
        )  # fmt: skip

    log = (tmp_path / "run.log").open("w")
    engine = subprocess.Popen(
        [*vcam, "run", "-c", str(config), "--control", "--control-port", str(control)],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=tmp_path,
    )  # fmt: skip
    reader = None
    try:
        descriptors = tmp_path / "state" / "control"
        wait_until(lambda: any(descriptors.glob("*.json")), timeout=60)

        def cam1_state() -> str:
            shown = cli("camera", "get", "cam1")
            return json.loads(shown.stdout)["camera"]["state"] if shown.returncode == 0 else ""

        wait_until(lambda: cam1_state() == "running", timeout=60)
        cam1 = json.loads(cli("camera", "get", "cam1").stdout)["camera"]
        publisher_pid = cam1["runtime"]["pid"]
        status = json.loads(cli("status", "--json").stdout)
        server_pid = status["servers"][0]["pid"]
        assert status["pid"] == engine.pid

        progress = tmp_path / "reader.progress"
        reader = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i",
             f"rtsp://127.0.0.1:{rtsp}/cam1", "-f", "null", "-", "-progress", str(progress)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        wait_until(lambda: progress.exists() and _progress_frames(progress) > 25)

        added = cli("camera", "add", "clip.mp4", "--name", "cam2")
        assert added.returncode == 0, added.stdout + added.stderr
        before = _progress_frames(progress)

        decode = subprocess.run(
            ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i",
             f"rtsp://127.0.0.1:{rtsp}/cam2", "-frames:v", "30", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        assert decode.returncode == 0, decode.stderr
        wait_until(lambda: _progress_frames(progress) > before + 25)
        assert reader.poll() is None, "the unaffected reader was disconnected"
        cam1 = json.loads(cli("camera", "get", "cam1").stdout)["camera"]
        assert cam1["runtime"]["pid"] == publisher_pid
        text = config.read_text()
        assert "# keep me" in text and "name: cam2" in text

        # The owned config refuses offline writers while the engine runs.
        offline = cli("add", "clip.mp4", "--name", "cam3", "--offline")
        assert offline.returncode != 0

        # Persistent and transient changes must both leave cam1's reader alone.
        for args in (("camera", "edit", "cam2", "--set", "start_offset=1"),
                     ("camera", "stop", "cam2"), ("camera", "start", "cam2"),
                     ("camera", "disable", "cam2"), ("camera", "enable", "cam2"),
                     ("camera", "remove", "cam2")):  # fmt: skip
            previous = config.read_bytes()
            changed = cli(*args, "--json")
            assert changed.returncode == 0, changed.stdout + changed.stderr
            transient = args[1] in {"stop", "start"}
            assert json.loads(changed.stdout)["config_persisted"] is not transient
            if transient:
                assert config.read_bytes() == previous
            mark = _progress_frames(progress)
            wait_until(lambda mark=mark: _progress_frames(progress) > mark + 25)
            assert reader.poll() is None, f"reader disconnected by {args}"
        cam1 = json.loads(cli("camera", "get", "cam1").stdout)["camera"]
        assert cam1["runtime"]["pid"] == publisher_pid
        final = json.loads(cli("status", "--json").stdout)
        assert (
            final["instance_id"] == status["instance_id"]
            and final["servers"][0]["pid"] == server_pid
        )
        assert "name: cam2" not in config.read_text()
    finally:
        engine.send_signal(signal.SIGINT)
        try:
            assert engine.wait(timeout=30) == 0
        finally:
            if engine.poll() is None:
                engine.kill()
                engine.wait(timeout=5)
            if reader is not None:
                try:
                    reader.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    reader.kill()
                    reader.wait(timeout=5)
            log.close()
    assert not list((tmp_path / "state" / "control").glob("*.json"))


def test_control_requires_confirmation_for_sync_reload_and_restores_it_after_restart(
    tmp_path: Path,
) -> None:
    import sys

    from captures import rtp_series, write_udp_capture
    from vcam.control import read_token_file
    from vcam.control_client import ControlClient, ControlClientError

    pytest.importorskip("av")
    resolve_binary(allow_download=False)
    clip = tmp_path / "scene.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15",
         "-t", "2", "-c:v", "libx264", "-x264-params",
         "keyint=15:scenecut=0:open-gop=0:bframes=0", str(clip)],
        check=True, timeout=30,
    )  # fmt: skip
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        replay_port = reservation.getsockname()[1]
        rtsp = _free_port()
    write_udp_capture(tmp_path / "scene.pcap", packets=rtp_series(5))
    config = tmp_path / "cameras.yaml"
    prefix = (
        f"control: {{enabled: true, port: 0}}\nserver: {{host: 127.0.0.1, rtsp_port: {rtsp}}}\n"
    )
    config.write_text(prefix + "cameras: []\n")
    state = tmp_path / "state"
    env = {**os.environ, "VCAM_STATE_DIR": str(state)}
    identity = None
    for launch in range(2):
        with (tmp_path / f"run-{launch}.log").open("w+") as log:
            daemon = subprocess.Popen(
                [sys.executable, "-m", "vcam", "run", "-c", str(config)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=tmp_path,
            )
            try:
                wait_until(
                    lambda d=daemon: any((state / "control").glob("*.json")) or d.poll() is not None
                )
                assert daemon.poll() is None, (tmp_path / f"run-{launch}.log").read_text()
                descriptor = json.loads(next((state / "control").glob("*.json")).read_text())
                client = ControlClient(
                    descriptor["endpoint"], token=read_token_file(Path(descriptor["token_file"]))
                )
                if launch == 0:
                    identity = descriptor["instance_id"]
                    config.write_text(
                        prefix
                        + "cameras:\n"
                        + "".join(
                            f"  - {{name: {name}, source: scene.mp4, sync_group: scene}}\n"
                            for name in ("left", "right")
                        )
                        + "replays:\n"
                        + f"  - {{name: capture, source: scene.pcap, port: {replay_port}}}\n"
                    )
                    plan = client.request("POST", "config/reload", {"preview": True})
                    assert plan["disruptive"] and plan["reset_sync_groups"] == ["scene"]
                    assert plan["restart_replays"] == [{"port": replay_port, "name": "capture"}]
                    body = {"plan_id": plan["plan_id"]}
                    with pytest.raises(ControlClientError) as denied:
                        client.request("POST", "config/apply", body, revision=plan["base_revision"])
                    assert denied.value.code == "disruption_not_acknowledged"
                    assert client.request("GET", "cameras")["cameras"] == []
                    op = client.request(
                        "POST",
                        "config/apply",
                        {**body, "allow_disruption": True},
                        revision=plan["base_revision"],
                    )
                    assert client.wait(op["id"], timeout=60)["applied"]
                else:
                    assert descriptor["instance_id"] != identity
                wait_until(
                    lambda c=client: c.request("GET", "status")["counts"]["running"] == 2,
                    timeout=60,
                )
                group = client.request("GET", "sync-groups/scene")["sync_group"]
                assert set(group["members"]) == {"left", "right"} and group["epoch_ns"]
                replay = client.request("GET", f"replays/{replay_port}/capture")["replay"]
                assert replay["state"] == "running"
                with socket.create_connection(("127.0.0.1", replay_port), timeout=5) as listener:
                    listener.sendall(
                        f"DESCRIBE {replay['url']} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode()
                    )
                    with listener.makefile("rb") as response:
                        assert response.readline().startswith(b"RTSP/1.0 200 ")
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-rtsp_transport",
                        "tcp",
                        "-i",
                        f"rtsp://127.0.0.1:{rtsp}/left",
                        "-frames:v",
                        "5",
                        "-f",
                        "null",
                        "-",
                    ],
                    check=True,
                    timeout=20,
                )
            finally:
                daemon.terminate()
                try:
                    assert daemon.wait(timeout=20) == 0
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                    raise
        assert not list((state / "control").glob("*.json"))
