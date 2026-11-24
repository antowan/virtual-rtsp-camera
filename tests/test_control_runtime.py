"""Owner-side runtime behaviour behind the control API: change planning,
granular apply, held cameras, the private MediaMTX client and CLI discovery."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import typer

from vcam import control_cli
from vcam import supervisor as supervisor_module
from vcam.mediamtx import MediaMTXAPIError, MediaMTXClient
from vcam.models import CameraSpec, CameraStack
from vcam.reconcile import plan_changes
from vcam.supervisor import ApplyError, ManagedProcess, Supervisor

# ---------------------------------------------------------------------------
# plan_changes
# ---------------------------------------------------------------------------


def test_same_name_on_two_ports_is_two_cameras(video_file: Path) -> None:
    before = CameraStack(cameras=[CameraSpec(name="cam", source=video_file)])
    after = CameraStack(
        cameras=[
            CameraSpec(name="cam", source=video_file),
            CameraSpec(name="cam", source=video_file, port=8600),
        ]
    )
    plan = plan_changes(before, after)
    assert plan.added == ((8600, "cam"),)
    assert not plan.removed and not plan.updated and not plan.disruptive


def test_port_move_is_remove_plus_add(video_file: Path) -> None:
    before = CameraStack(cameras=[CameraSpec(name="cam", source=video_file)])
    after = CameraStack(cameras=[CameraSpec(name="cam", source=video_file, port=8600)])
    plan = plan_changes(before, after)
    assert (plan.added, plan.removed) == (((8600, "cam"),), ((8554, "cam"),))


def test_disabling_a_camera_removes_it(video_file: Path) -> None:
    before = CameraStack(cameras=[CameraSpec(name="cam", source=video_file)])
    after = CameraStack(cameras=[CameraSpec(name="cam", source=video_file, enabled=False)])
    assert plan_changes(before, after).removed == ((8554, "cam"),)


def test_server_and_sync_changes_are_disruptive(video_file: Path) -> None:
    before = CameraStack(cameras=[CameraSpec(name="cam", source=video_file)])
    server = before.model_copy(update={"server": before.server.model_copy(update={"api_port": 1})})
    assert plan_changes(before, server).disruptive_reasons == ("server settings changed",)
    pair = [CameraSpec(name=n, source=video_file, sync_group="g") for n in ("a", "b")]
    synced = CameraStack(cameras=pair)
    widened = CameraStack(cameras=[*pair, CameraSpec(name="c", source=video_file, sync_group="g")])
    assert plan_changes(synced, widened).disruptive


def test_unchanged_stack_plans_nothing(stack: CameraStack) -> None:
    assert plan_changes(stack, stack).empty


# ---------------------------------------------------------------------------
# granular apply
# ---------------------------------------------------------------------------


class _RecordingClient:
    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, api_url: str, timeout: float = 3.0) -> None:
        self.api_url = api_url

    def add_path(self, name: str) -> None:
        self.calls.append(("add", name))

    def delete_path(self, name: str) -> None:
        self.calls.append(("delete", name))


@pytest.fixture
def running(
    stack: CameraStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Supervisor, dict[str, bool]]]:
    """A supervisor whose children are faked; ``outcomes`` forces start results."""
    _RecordingClient.calls = []
    outcomes: dict[str, bool] = {}
    started: list[str] = []

    def fake_start(self: ManagedProcess) -> bool:
        if self.before_start is not None:
            self.before_start()
        started.append(self.name)
        return outcomes.get(self.name, True)

    monkeypatch.setattr(supervisor_module, "MediaMTXClient", _RecordingClient)
    monkeypatch.setattr(ManagedProcess, "start", fake_start)
    monkeypatch.setattr(supervisor_module, "_wait_for_port", lambda *a, **k: True)
    binary = tmp_path / "mediamtx"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    supervisor = Supervisor(stack, binary, work_dir=tmp_path / "work", verify=False)
    supervisor.prepare()
    assert supervisor._start_engine()
    yield supervisor, outcomes
    supervisor._engine_stop()


def _edited(stack: CameraStack, video_file: Path) -> CameraStack:
    cameras = [
        stack.cameras[0],
        stack.cameras[1].model_copy(update={"start_offset": 1.0}),
        CameraSpec(name="cam3", source=video_file),
    ]
    return stack.model_copy(update={"cameras": cameras})


def test_apply_only_declares_new_paths_and_leaves_live_config_alone(
    running: tuple[Supervisor, dict[str, bool]], video_file: Path
) -> None:
    supervisor, _ = running
    unaffected = supervisor.runtime_for((8554, "cam1"))
    config = supervisor.instances[0].config_path
    assert config is not None
    before = config.read_bytes()

    result = supervisor._apply(_edited(supervisor.stack, video_file), full=False)

    assert result["mode"] == "granular"
    # cam2 kept its path while its publisher was replaced: no duplicate add.
    assert _RecordingClient.calls == [("add", "cam3")]
    assert supervisor.runtime_for((8554, "cam1")) is unaffected
    # The running MediaMTX watches its config: granular changes never rewrite it.
    assert config.read_bytes() == before
    # ...but a restart of that server regenerates it with the current paths.
    supervisor.servers[0].start()
    assert "cam3" in config.read_text()


def test_failed_camera_start_is_reported_not_applied(
    running: tuple[Supervisor, dict[str, bool]], video_file: Path
) -> None:
    supervisor, outcomes = running
    outcomes["cam3"] = False
    with pytest.raises(ApplyError) as raised:
        supervisor._apply(_edited(supervisor.stack, video_file), full=False)
    assert "cam3@8554" in raised.value.failures
    assert supervisor.runtime_for((8554, "cam3")) is None
    assert ("delete", "cam3") in _RecordingClient.calls


def test_partial_apply_is_retried_by_reapply_or_camera_start(
    running: tuple[Supervisor, dict[str, bool]], video_file: Path
) -> None:
    supervisor, outcomes = running
    target = _edited(supervisor.stack, video_file)
    outcomes["cam3"] = False
    with pytest.raises(ApplyError):
        supervisor._apply(target, full=False)
    outcomes["cam3"] = True
    # The same (already persisted) stack again: nothing differs, cam3 is retried.
    supervisor._apply(target, full=False)
    assert supervisor.runtime_for((8554, "cam3")) is not None

    supervisor._remove_camera((8554, "cam3"), keep_path=False)
    supervisor._camera_action((8554, "cam3"), "start")
    assert supervisor.runtime_for((8554, "cam3")) is not None


def test_held_camera_stays_stopped_across_engine_stop_start(
    running: tuple[Supervisor, dict[str, bool]],
) -> None:
    supervisor, _ = running
    supervisor._camera_action((8554, "cam1"), "stop")
    supervisor._engine_action("restart")

    held = supervisor.runtime_for((8554, "cam1"))
    assert held is not None and held.process.held
    other = supervisor.runtime_for((8554, "cam2"))
    assert other is not None and not other.process.held

    supervisor._camera_action((8554, "cam1"), "start")
    assert not held.process.held and not supervisor.held_keys


def test_failed_manual_start_raises(running: tuple[Supervisor, dict[str, bool]]) -> None:
    supervisor, outcomes = running
    outcomes["cam1"] = False
    with pytest.raises(supervisor_module.SupervisorError, match="failed to start"):
        supervisor._camera_action((8554, "cam1"), "restart")


def test_reapply_retries_a_failed_private_path_removal(running, monkeypatch) -> None:
    supervisor, _ = running
    original = supervisor.runtime_for((8554, "cam1"))
    target = supervisor.stack.model_copy(update={"cameras": [supervisor.stack.cameras[0]]})
    with monkeypatch.context() as patch:

        def fail(self, name):
            raise MediaMTXAPIError("private API unavailable")

        patch.setattr(_RecordingClient, "delete_path", fail)
        with pytest.raises(ApplyError):
            supervisor._apply(target, full=False)
        assert supervisor.runtime_for((8554, "cam2")).process.held
    supervisor._apply(target, full=False)
    assert supervisor.runtime_for((8554, "cam2")) is None
    assert ("delete", "cam2") in _RecordingClient.calls
    assert supervisor.runtime_for((8554, "cam1")) is original


def test_apply_reuses_media_from_preflight(running, video_file, h264_info, monkeypatch) -> None:
    from vcam.reconcile import preflight

    supervisor, _ = running
    target = _edited(supervisor.stack, video_file)
    probes = []

    def probe(path, **kwargs):
        probes.append(path)
        return h264_info

    monkeypatch.setattr("vcam.reconcile.probe", probe)
    admission = preflight(target, plan_changes(supervisor.stack, target), "ffprobe")
    assert probes == [video_file]
    monkeypatch.setattr(
        supervisor_module, "try_probe", lambda *a, **kw: pytest.fail("duplicate owner-side ffprobe")
    )
    supervisor._apply(target, full=False, admission=admission)
    assert supervisor.runtime_for((8554, "cam3")).info == h264_info


# ---------------------------------------------------------------------------
# MediaMTXClient
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_api() -> Iterator[tuple[str, dict[str, tuple[int, Any]]]]:
    responses: dict[str, tuple[int, Any]] = {}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            status, body = responses.get(f"{self.command} {self.path}", (404, None))
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_DELETE = _reply

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="fake-mediamtx-api")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_mediamtx_client_is_idempotent_and_strict(
    fake_api: tuple[str, dict[str, tuple[int, Any]]],
) -> None:
    url, responses = fake_api
    client = MediaMTXClient(url)
    responses["POST /v3/config/paths/add/cam"] = (400, {"error": "path already exists"})
    client.add_path("cam")
    client.delete_path("gone")  # 404: already absent
    responses["POST /v3/config/paths/add/bad"] = (400, {"error": "invalid name"})
    with pytest.raises(MediaMTXAPIError):
        client.add_path("bad")
    responses["GET /v3/paths/list?itemsPerPage=10000"] = (
        200,
        {"items": [{"name": "cam", "ready": True}]},
    )
    assert client.list_paths() == {"cam": {"name": "cam", "ready": True}}


def test_mediamtx_client_unreachable_raises() -> None:
    with pytest.raises(MediaMTXAPIError, match="unreachable"):
        MediaMTXClient("http://127.0.0.1:9", timeout=0.5).add_path("cam")


# ---------------------------------------------------------------------------
# control CLI
# ---------------------------------------------------------------------------


def test_build_patch_nests_assignments_and_unsets() -> None:
    patch = control_cli.build_patch(["video.fps=10", "loop=false", "label=a b"], ["audio"])
    assert patch == {"video": {"fps": 10}, "loop": False, "label": "a b", "audio": None}
    with pytest.raises(typer.Exit):
        control_cli.build_patch(["fps"], [])


def test_discover_without_owner_allows_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VCAM_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "cameras.yaml"
    config.write_text("cameras: []\n")
    assert control_cli.discover(config, required=False) is None
    with pytest.raises(typer.Exit):
        control_cli.discover(config, required=True)


def test_discover_refuses_an_endpoint_of_another_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vcam import control

    config = tmp_path / "cameras.yaml"
    config.write_text("cameras: []\n")
    descriptor = {
        "instance_id": "mine",
        "pid": 1,
        "endpoint": "http://127.0.0.1:1/api/v1",
        "token_file": str(tmp_path / "token"),
    }
    monkeypatch.setattr(control, "read_descriptor", lambda path: descriptor)
    monkeypatch.setattr(control, "pid_alive", lambda pid: True)
    monkeypatch.setattr(control_cli, "_read_token", lambda path: "t")
    status = {"api_version": 1, "instance_id": "someone-else", "config": {"path": str(config)}}
    monkeypatch.setattr(control_cli.ControlClient, "request", lambda self, *a, **k: status)
    with pytest.raises(typer.Exit):
        control_cli.discover(config, required=False)
    assert "not the instance that owns it" in " ".join(capsys.readouterr().err.split())


def test_explicit_endpoint_failure_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    monkeypatch.setattr(control_cli, "_read_token", lambda path: "t")
    with pytest.raises(typer.Exit):
        control_cli.connect(None, "http://127.0.0.1:9/api/v1", token, None)
