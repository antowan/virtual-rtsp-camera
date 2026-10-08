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
