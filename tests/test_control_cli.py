"""CLI routing and failure semantics when a local YAML is also available."""

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vcam import cli, control_cli
from vcam.control_client import ControlClient, ControlClientError

runner = CliRunner()
STATUS = {"api_version": 1, "instance_id": "fixture-engine"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "240")
    token = tmp_path / "operator.token"
    token.touch(mode=0o600)
    token.write_text("fixture-token\n")
    monkeypatch.setenv("VCAM_CONTROL_TOKEN_FILE", str(token))
    config = tmp_path / "cameras.yaml"
    config.write_bytes(b"# not the remote config\r\ncameras: [{name: local, source: old.mp4}]\r\n")
    return config


@pytest.mark.parametrize("selector", ["--endpoint", "VCAM_ENDPOINT", "VCAM_CONTROL_ENDPOINT"])
def test_unreachable_endpoint_never_edits_local_yaml(workspace, monkeypatch, selector):
    original = workspace.read_bytes()
    files = set(workspace.parent.iterdir())
    # Keep the port reserved but not listening: no external server or timing race.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
        args = ["add", "remote.mp4", "--json"]
        if selector == "--endpoint":
            args.extend([selector, endpoint])
        else:
            monkeypatch.setenv(selector, endpoint)
        failed = runner.invoke(cli.app, args)
    assert failed.exit_code == 1 and not failed.stdout
    assert "[unavailable]" in failed.stderr
    assert workspace.read_bytes() == original and set(workspace.parent.iterdir()) == files


@pytest.mark.parametrize(("failure", "diagnostic"), [("auth", "unauthorized"), ("version", "v2")])
def test_unverified_endpoint_never_receives_a_write(workspace, monkeypatch, failure, diagnostic):
    original = workspace.read_bytes()
    seen = []

    def handshake(_client, method, path, **kwargs):
        seen.append((method, path))
        if failure == "auth":
            raise ControlClientError("denied", code="unauthorized", status=401)
        return {**STATUS, "api_version": 2}

    monkeypatch.setattr(ControlClient, "request", handshake)
    result = runner.invoke(cli.app, ["add", "remote.mp4", "--endpoint", "http://localhost:12345"])
    assert result.exit_code != 0 and not result.stdout and diagnostic in result.stderr
    assert seen == [("GET", "status")] and workspace.read_bytes() == original


@pytest.mark.parametrize("local", [["--offline"], ["-c", "cameras.yaml"]])
def test_local_selector_rejects_remote_from_environment(workspace, monkeypatch, local):
    monkeypatch.setenv("VCAM_ENDPOINT", "http://localhost:12345")
    result = runner.invoke(cli.app, ["list", "--json", *local])
    assert result.exit_code == 2 and not result.stdout
    assert "$VCAM_ENDPOINT" in result.stderr


def test_remote_add_preserves_source_and_reports_persisted_but_unapplied(workspace, monkeypatch):
    workspace.unlink()  # This client has neither the config nor the clip.
    seen = []
    # An explicit flag must win even when a different remote is named by the environment.
    monkeypatch.setenv("VCAM_ENDPOINT", "http://localhost:12346")

    def request(client, method, path, payload=None, **headers):
        assert client.base_url == "http://localhost:12345/api/v1"
        if path == "status":
            return STATUS
        if path == "cameras" and method == "GET":
            return {"cameras": [], "revision": "r1"}
        if path == "cameras" and method == "POST":
            seen.append((payload["source"], headers["revision"]))
            return {"id": "edit", "status": "queued"}
        assert (method, path) == ("GET", "operations/edit")
        return {
            "id": "edit",
            "status": "failed",
            "config_persisted": True,
            "applied": False,
            "error": {"code": "stream_failed", "message": "publisher did not start"},
        }

    monkeypatch.setattr(ControlClient, "request", request)
    args = ["add", "engine/clip.mp4", "--endpoint", "http://localhost:12345", "--json"]
    queued = runner.invoke(cli.app, [*args, "--no-wait"])
    assert queued.exit_code == 0 and json.loads(queued.stdout)["status"] == "queued"
    failed = runner.invoke(cli.app, args)
    assert failed.exit_code == 1 and not failed.stdout
    assert "[stream_failed]" in failed.stderr and "NOT running" in failed.stderr
    assert seen == [("engine/clip.mp4", "r1")] * 2 and not workspace.exists()


def test_host_override_is_resolved_by_discovered_engine(workspace, monkeypatch):
    url = "rtsp://viewer.example:8555/side"
    stream = {"port": 8555, "name": "side", "kind": "camera", "url": url}

    def request(_client, method, path):
        assert method == "GET"
        if path == "cameras":
            return {"revision": "r1", "cameras": [{"port": 8555, "name": "side", "enabled": False}]}
        if path == "replays":
            return {"replays": []}
        assert path == "urls?host=viewer.example&include_disabled=true"
        return {"urls": [url], "streams": [stream]}

    client = ControlClient("http://localhost:12345", token="fixture")
    monkeypatch.setattr(ControlClient, "request", request)
    monkeypatch.setattr(
        control_cli, "discover", lambda *a, **kw: control_cli.Remote(client, STATUS)
    )
    args = ["--host", "viewer.example", "--json"]
    listing = runner.invoke(cli.app, ["list", *args])
    urls = runner.invoke(cli.app, ["urls", "--all", *args])
    assert listing.exit_code == urls.exit_code == 0
    assert json.loads(listing.stdout)["cameras"][0]["url"] == url
    assert json.loads(urls.stdout)["urls"] == [url]


def test_diagnostics_remain_local_unless_targeted(workspace, monkeypatch, h264_info):
    monkeypatch.setattr(
        control_cli, "discover", lambda *a, **kw: pytest.fail("unexpected discovery")
    )
    monkeypatch.setattr(cli, "probe_source", lambda *a, **kw: h264_info)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(cli.binaries, "resolve_binary", lambda *a, **kw: Path("/tools/mediamtx"))
    local_probe = runner.invoke(cli.app, ["probe", "clip.mp4", "--json"])
    local_doctor = runner.invoke(cli.app, ["doctor", "--json"])
    assert local_probe.exit_code == local_doctor.exit_code == 0
    assert json.loads(local_probe.stdout)["codec"] == "h264"
    assert json.loads(local_doctor.stdout)["tools"]["mediamtx"]

    def request(_client, method, path, payload=None):
        if path == "status":
            return STATUS
        assert method == "POST"
        if path == "diagnostics/probe":
            assert payload == {"source": "engine/clip.mp4"}
            return {"codec": "hevc"}
        assert path == "diagnostics/doctor"
        return {"ok": False, "tools": {"mediamtx": False}}

    monkeypatch.setattr(ControlClient, "request", request)
    monkeypatch.setenv("VCAM_CONTROL_ENDPOINT", "http://localhost:12345")
    remote_probe = runner.invoke(cli.app, ["probe", "engine/clip.mp4", "--json"])
    remote_doctor = runner.invoke(cli.app, ["doctor", "--json"])
    assert remote_probe.exit_code == 0 and json.loads(remote_probe.stdout)["codec"] == "hevc"
    assert remote_doctor.exit_code == 1 and not json.loads(remote_doctor.stdout)["ok"]
