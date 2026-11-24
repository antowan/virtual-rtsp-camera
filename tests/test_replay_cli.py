"""CLI surface of the replay feature (TEST-012)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from captures import rtp_series, write_udp_capture
from vcam.cli import app
from vcam.supervisor import REPLAY_PASSWORD_ENV

runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("TERM", "dumb")


def invoke(*args: str):
    return runner.invoke(app, list(args))


@pytest.fixture
def capture(tmp_path: Path) -> Path:
    path = tmp_path / "camera.pcap"
    write_udp_capture(path, packets=rtp_series(5))
    return path


def test_replay_appears_in_the_help() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert "replay" in result.output


def test_list_tracks_describes_without_serving(capture: Path) -> None:
    result = invoke("replay", str(capture), "--list-tracks")
    assert result.exit_code == 0
    assert "packets:   5" in result.output
    assert "video pt=96" in result.output
    assert "handshake: yes" in result.output


def test_list_tracks_reports_a_guessed_description(tmp_path: Path) -> None:
    path = tmp_path / "raw.pcap"
    write_udp_capture(path, packets=rtp_series(3), with_handshake=False)
    result = invoke("replay", str(path), "--list-tracks")
    assert result.exit_code == 0
    assert "no (guessed SDP)" in result.output
    assert "no usable RTSP handshake" in result.output


def test_missing_capture_is_an_error(tmp_path: Path) -> None:
    result = invoke("replay", str(tmp_path / "absent.pcap"), "--list-tracks")
    assert result.exit_code == 1
    assert "capture file not found" in result.output


def test_missing_sdp_override_is_an_error(capture: Path, tmp_path: Path) -> None:
    result = invoke("replay", str(capture), "--sdp", str(tmp_path / "absent.sdp"), "--list-tracks")
    assert result.exit_code == 1
    assert "SDP file not found" in result.output


def test_a_password_without_a_username_is_rejected(capture: Path) -> None:
    result = invoke("replay", str(capture), "--password", "hunter2", "--list-tracks")
    assert result.exit_code == 1
    assert "must be given together" in result.output


def test_a_username_without_a_secret_anywhere_is_rejected(capture: Path) -> None:
    result = invoke("replay", str(capture), "--username", "admin", "--list-tracks")
    assert result.exit_code == 1
    assert "must be given together" in result.output
    assert REPLAY_PASSWORD_ENV in result.output


def test_the_secret_can_come_from_the_environment(
    capture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How the supervisor passes it, so it never lands in argv."""
    monkeypatch.setenv(REPLAY_PASSWORD_ENV, "s3cret")
    result = invoke("replay", str(capture), "--username", "admin", "--list-tracks")
    assert result.exit_code == 0


def test_an_unreadable_capture_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "garbage.pcap"
    path.write_bytes(b"definitely not a capture")
    result = invoke("replay", str(path), "--list-tracks")
    assert result.exit_code == 1
    assert "error" in result.output.lower()


def test_a_capture_without_rtp_is_reported(tmp_path: Path) -> None:
    from vcam.pcap import PcapWriter

    path = tmp_path / "noise.pcap"
    with PcapWriter(path) as writer:
        writer.write_udp(b"nothing useful", 1.0, src=("10.0.0.1", 1234), dst=("10.0.0.2", 5678))
    result = invoke("replay", str(path), "--list-tracks")
    assert result.exit_code == 1
    assert "no RTP stream" in result.output


def test_doctor_reports_the_pcap_backend() -> None:
    result = invoke("doctor")
    assert "pcap backend" in result.output


def test_list_shows_replays(tmp_path: Path, capture: Path) -> None:
    config = tmp_path / "cameras.yaml"
    config.write_text(
        f"replays:\n  - name: fault\n    source: {capture}\n    port: 8555\n", encoding="utf-8"
    )
    result = invoke("list", "--config", str(config), "--host", "cam.local")
    assert result.exit_code == 0
    assert "Capture replays" in result.output
    assert "rtsp://cam.local:8555/fault" in result.output


def test_urls_includes_replays(tmp_path: Path, capture: Path) -> None:
    config = tmp_path / "cameras.yaml"
    config.write_text(
        f"replays:\n  - name: fault\n    source: {capture}\n    port: 8555\n", encoding="utf-8"
    )
    result = invoke("urls", "--config", str(config), "--host", "cam.local")
    assert result.exit_code == 0
    assert result.output.strip() == "rtsp://cam.local:8555/fault"


class _RecordingSupervisor:
    """Stands in for Supervisor so `vcam run` can be driven without processes."""

    created: ClassVar[list[Path | None]] = []

    def __init__(self, stack, mediamtx_binary, **_kwargs) -> None:
        type(self).created.append(mediamtx_binary)

    def run(self) -> int:
        return 0

    def shutdown(self) -> None:
        pass


@pytest.fixture
def recording_supervisor(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    created: list[Path | None] = []
    monkeypatch.setattr(_RecordingSupervisor, "created", created)
    monkeypatch.setattr("vcam.cli.Supervisor", _RecordingSupervisor)
    return created


def test_run_serves_a_replay_only_config_without_mediamtx(
    capture: Path, recording_supervisor: list[Path | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_mediamtx(*_args, **_kwargs):
        raise AssertionError("a replay-only stack must not resolve MediaMTX")

    monkeypatch.setattr("vcam.cli.binaries.resolve_binary", no_mediamtx)
    config = capture.parent / "cameras.yaml"
    config.write_text(f"replays:\n  - name: fault\n    source: {capture.name}\n    port: 8655\n")

    result = invoke("run", "-c", str(config), "--no-download")

    assert result.exit_code == 0, result.output
    assert recording_supervisor == [None]


def test_run_resolves_mediamtx_when_a_replay_stack_has_an_ingest(
    capture: Path,
    tmp_path: Path,
    recording_supervisor: list[Path | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "mediamtx"
    monkeypatch.setattr("vcam.cli.binaries.resolve_binary", lambda *_a, **_k: binary)
    config = capture.parent / "cameras.yaml"
    config.write_text(
        "ingest:\n  rtsp_port: 8656\n"
        f"replays:\n  - name: fault\n    source: {capture.name}\n    port: 8655\n"
    )

    result = invoke("run", "-c", str(config))

    assert result.exit_code == 0, result.output
    assert recording_supervisor == [binary]
