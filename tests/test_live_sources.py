"""Simulator contract: URL preservation, input flags, import, ingest and retry."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from vcam.cli import app
from vcam.config import dump_stack, load_stack
from vcam.errors import ConfigError, ProbeError
from vcam.ffmpeg import build_publish_command, effective_mode
from vcam.mediamtx import plan_instances, render_server_config
from vcam.models import CameraSpec, CameraStack, IngestSpec, SimulationSpec, StreamMode
from vcam.probe import MediaInfo, probe
from vcam.sim_manifest import import_sim
from vcam.supervisor import LivePublisher, ManagedProcess, Supervisor

URL = "rtsp://127.0.0.1:8654/sim/anpr-front"
runner = CliRunner()


@pytest.mark.parametrize("url", [URL, "udp://127.0.0.1:5000?fifo_size=1000000"])
def test_live_sources_survive_config_roundtrip(tmp_path: Path, url: str) -> None:
    camera = CameraSpec(name="front", source=url, loop=False, realtime=False)
    assert camera.is_live
    path = tmp_path / "nested" / "cameras.yaml"
    path.parent.mkdir()
    path.write_text(dump_stack(CameraStack(cameras=[camera])))
    assert load_stack(path).cameras[0].source == url


@pytest.mark.parametrize(
    "url", ["rtsp://", "udp://host:0", "udp://host", "rtsp://host:65536", "srt://host:123"]
)
def test_invalid_live_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        CameraSpec(name="front", source=url)


def test_files_remain_paths() -> None:
    camera = CameraSpec(name="front", source="~/clip.mp4")
    assert camera.source == Path("~/clip.mp4").expanduser()
    assert not camera.is_live


@pytest.mark.parametrize("source", [None, 42, {}])
def test_source_type_errors_are_validation_errors(source: object) -> None:
    with pytest.raises(ValidationError, match="source must"):
        CameraSpec.model_validate({"name": "front", "source": source})


def test_live_sync_and_seek_are_rejected() -> None:
    with pytest.raises(ValidationError, match="file-replay only"):
        CameraSpec(name="front", source=URL, sync_group="scene")
    with pytest.raises(ValidationError, match="start_offset"):
        CameraSpec(name="front", source=URL, start_offset=1)


@pytest.mark.parametrize("url", [URL, "udp://127.0.0.1:5000"])
def test_live_input_flags_and_auto_copy(url: str) -> None:
    camera = CameraSpec(name="front", source=url, source_timeout=3)
    command = build_publish_command(camera, "rtsp://127.0.0.1:8554/front")
    inputs = command[: command.index("-i")]
    assert "-re" not in inputs
    assert "-stream_loop" not in inputs
    assert inputs[inputs.index("-fflags") + 1] == "nobuffer"
    assert inputs[inputs.index("-timeout") + 1] == "3000000"
    assert ("-rtsp_transport" in inputs) == url.startswith("rtsp://")
    assert command[command.index("-i") + 1] == url
    assert command[command.index("-c:v") + 1] == "copy"
    # The live contract selects copy even if a probe reports another codec.
    assert effective_mode(camera, MediaInfo(path=url, codec="mpeg4")) == StreamMode.COPY


@pytest.mark.parametrize("fault", ["noise", "blackout", "degraded", "stutter", "frozen", "flaky"])
def test_fault_only_transcodes_its_live_camera(fault: str) -> None:
    clean = CameraSpec(name="clean", source=URL)
    broken = CameraSpec(
        name="broken", source=URL, simulation=SimulationSpec.model_validate({"mode": fault})
    )
    assert effective_mode(clean, None) == StreamMode.COPY
    assert effective_mode(broken, None) == StreamMode.TRANSCODE


def test_live_probe_is_bounded_and_uses_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.probe.shutil.which", lambda _: "/ffprobe")

    def run(command, **kwargs):
        assert kwargs["timeout"] == 3
        assert command[command.index("-rtsp_transport") + 1] == "tcp"
        assert command[command.index("-timeout") + 1] == "3000000"
        assert command[command.index("-analyzeduration") + 1] == "500000"
        assert command[command.index("-probesize") + 1] == "1000000"
        assert command[-1] == URL
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264"}]}).encode(),
        )

    monkeypatch.setattr("vcam.probe.subprocess.run", run)
    assert probe(URL, timeout=3).codec == "h264"


def test_probe_timeout_reports_redacted_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.probe.shutil.which", lambda _: "/ffprobe")

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("vcam.probe.subprocess.run", run)
    with pytest.raises(ProbeError, match="timed out") as error:
        probe(URL.replace("127.0.0.1", "sim:secret@127.0.0.1"), timeout=0.1)
    assert "secret" not in str(error.value)


def manifest_file(tmp_path: Path) -> Path:
    path = tmp_path / "sim-streams.json"
    path.write_text(
        json.dumps(
            {
                "schema": "eais-sim-streams/1",
                "run_id": "example",
                "ground_truth": {"websocket": None, "events_url": "http://localhost/events"},
                "cameras": [
                    {
                        "id": "anpr-front",
                        "ingest_url": URL,
                        "codec": "h264",
                        "profile": "high",
                        "width": 1920,
                        "height": 1080,
                        "fps": 30,
                        "gop": 30,
                        "bitrate_kbps": 8000,
                        "pose": {"position": [1, 2, 3]},
                    }
                ],
            }
        )
    )
    return path


def test_import_maps_contract_without_connecting(tmp_path: Path) -> None:
    path = manifest_file(tmp_path)
    stack = import_sim(path)
    camera = stack.cameras[0]
    assert (camera.name, camera.source) == ("anpr-front", URL)
    assert camera.video.resolution == "1920x1080"
    assert camera.video.fps == 30
    assert camera.sync_group is None
    assert effective_mode(camera, None) == StreamMode.COPY
    result = runner.invoke(app, ["import-sim", str(path)])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.output)["cameras"][0]["source"] == URL
    output = tmp_path / "cameras.yaml"
    assert runner.invoke(app, ["import-sim", str(path), "-o", str(output)]).exit_code == 0
    assert load_stack(output) == stack
    assert runner.invoke(app, ["import-sim", str(path), "-o", str(output)]).exit_code == 1


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "eais-sim-streams/2"},
        {"cameras": []},
        {"cameras": [{"id": "missing-fields"}]},
        {"cameras": [{"id": "bad", "ingest_url": "file.mp4", "width": 1, "height": 1, "fps": 30}]},
    ],
)
def test_import_rejects_invalid_manifest(tmp_path: Path, change: dict) -> None:
    path = manifest_file(tmp_path)
    data = json.loads(path.read_text()) | change
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="invalid"):
        import_sim(path)
    assert runner.invoke(app, ["import-sim", str(path)]).exit_code == 1


def test_import_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = manifest_file(tmp_path)
    data = json.loads(path.read_text())
    data["cameras"] *= 2
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="duplicate"):
        import_sim(path)


@pytest.mark.parametrize(
    "args",
    [
        ["--source", URL],
        ["--camera", f"front={URL}"],
        ["--camera", "udp://127.0.0.1:5000?fifo_size=1000"],
    ],
)
def test_inline_live_dry_run_does_not_probe(args: list[str]) -> None:
    result = runner.invoke(app, ["run", *args, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "-c:v copy" in result.output
    assert "-stream_loop" not in result.output


def test_add_live_url(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text(dump_stack(CameraStack(cameras=[CameraSpec(name="other", source=URL)])))
    result = runner.invoke(app, ["add", URL, "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert load_stack(path).cameras[-1].source == URL


def test_ingest_is_separate_tcp_only_and_sim_publish_only() -> None:
    stack = CameraStack(ingest=IngestSpec(), cameras=[CameraSpec(name="front", source=URL)])
    instances = plan_instances(stack)
    assert [item.rtsp_port for item in instances] == [8554, 8654]
    assert len({item.api_port for item in instances}) == 2
    assert len({item.rtp_port for item in instances}) == 2
    config = render_server_config(instances[-1], stack.server, stack)
    assert config["rtspAddress"] == "127.0.0.1:8654"
    assert config["rtspTransports"] == ["tcp"]
    assert config["paths"] == {"~^sim/.+$": {}}
    publishers = [
        user
        for user in config["authInternalUsers"]
        if any(permission["action"] == "publish" for permission in user["permissions"])
    ]
    assert [user["user"] for user in publishers] == ["sim"]
    assert publishers[0]["ips"] == ["127.0.0.1", "::1"]


def test_ingest_port_collisions_rejected() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        CameraStack(
            ingest=IngestSpec(rtsp_port=8554), cameras=[CameraSpec(name="front", source=URL)]
        )


def test_supervisor_prepares_absent_live_sources(tmp_path: Path) -> None:
    stack = CameraStack(
        ingest=IngestSpec(),
        cameras=[CameraSpec(name="front", source=URL.replace("127.0.0.1", "sim:secret@127.0.0.1"))],
    )
    supervisor = Supervisor(stack, Path("mediamtx"), work_dir=tmp_path, verify=False)
    supervisor.prepare()
    assert len(supervisor.servers) == 2
    snapshot = supervisor.health_snapshot()
    assert snapshot["cameras"][0]["state"] == "waiting-for-source"
    assert "secret" not in str(snapshot)
    assert (tmp_path / "mediamtx-8654.yml").stat().st_mode & 0o777 == 0o600


def live_publisher(monkeypatch: pytest.MonkeyPatch, max_restarts: int = 0) -> LivePublisher:
    monkeypatch.setattr("vcam.supervisor.shutil.which", lambda _: "/ffprobe")
    return LivePublisher(
        CameraSpec(name="front", source=URL),
        "rtsp://127.0.0.1:8554/front",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        log_level="warning",
        max_restarts=max_restarts,
    )


def finish_check(publisher: LivePublisher) -> None:
    assert publisher._source_done.wait(1)
    publisher.tick(time.monotonic())


def test_source_outage_backoff_and_recovery_do_not_spend_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = live_publisher(monkeypatch)

    def unavailable(*args, **kwargs):
        raise ProbeError("not published yet")

    monkeypatch.setattr("vcam.supervisor.probe", unavailable)
    for failures in range(1, 8):
        before = time.monotonic()
        publisher.start()
        finish_check(publisher)
        assert publisher.state == "waiting-for-source"
        assert publisher.retry_at - before >= min(2 ** (failures - 1), 30)
        assert publisher.restarts == 0
        assert not publisher.gave_up

    monkeypatch.setattr("vcam.supervisor.probe", lambda *a, **kw: MediaInfo(path=URL, codec="h264"))
    launched = []
    monkeypatch.setattr(ManagedProcess, "start", lambda self: launched.append(self.command) or True)
    publisher.start()
    finish_check(publisher)
    assert publisher.state == "starting"
    assert launched[0][launched[0].index("-c:v") + 1] == "copy"
    # Recovering a previously launched camera after an outage also ignores max_restarts=0.
    publisher.source_failures = 1
    publisher.start()
    finish_check(publisher)
    assert len(launched) == 2
    assert not publisher.gave_up


def test_available_source_publisher_failure_respects_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = live_publisher(monkeypatch)
    monkeypatch.setattr("vcam.supervisor.probe", lambda *a, **kw: MediaInfo(path=URL, codec="h264"))
    monkeypatch.setattr(ManagedProcess, "start", lambda self: True)
    publisher.start()
    finish_check(publisher)
    publisher.start()
    finish_check(publisher)
    assert publisher.gave_up
    assert publisher.state == "failed"


def test_shutdown_joins_inflight_source_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = live_publisher(monkeypatch)

    def slow_probe(*args, **kwargs):
        time.sleep(0.05)
        raise ProbeError("offline")

    monkeypatch.setattr("vcam.supervisor.probe", slow_probe)
    publisher.start()
    publisher.stop()
    assert publisher._source_thread is None


def test_live_flaky_restart_is_planned_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = live_publisher(monkeypatch)
    monkeypatch.setattr("vcam.supervisor.probe", lambda *a, **kw: MediaInfo(path=URL, codec="h264"))
    monkeypatch.setattr(ManagedProcess, "start", lambda self: True)
    publisher.start()
    finish_check(publisher)
    publisher.suspended = True
    publisher.stop()
    publisher.suspended = False
    publisher.start()
    finish_check(publisher)
    assert not publisher.gave_up
    assert publisher.failed_restarts == 0
    assert publisher.restarts == 0
