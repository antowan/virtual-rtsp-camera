import contextlib
import json
import logging
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import typer

from vcam.config import parse_stack_text
from vcam.config_store import ConfigStore
from vcam.control import Controller, config_ownership, read_descriptor, read_token_file
from vcam.control_client import ControlClient, ControlClientError
from vcam.errors import SupervisorError
from vcam.models import ControlSpec
from vcam.supervisor import ApplyError, EngineCommand, ManagedProcess, Supervisor

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
from fastapi.testclient import TestClient

from vcam.control_api import API_PREFIX, ControlServer, create_app


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Controller:
    monkeypatch.setenv("VCAM_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "cameras.yaml"
    text = (
        "# this comment is not a read API field\n"
        "control: {enabled: true, port: 0, allowed_origins: ['https://ui.example']}\n"
        "server: {auth: {username: reader, password: private-example}}\ncameras: []\n"
    )
    document = ConfigStore(config).commit(text, expected_revision=None)
    stack = parse_stack_text(text, config, allow_empty=True)
    runtime = Supervisor(stack, None, work_dir=tmp_path / "runtime", allow_empty=True)
    controller = Controller(
        runtime,
        config,
        effective=lambda stack: stack,
        spec=stack.control,
        revision=document.revision,
    )
    controller.tick(runtime)
    return controller


def test_authenticated_contract_and_browser_preflight(owner: Controller) -> None:
    with TestClient(create_app(owner), base_url="http://localhost") as client:
        url = API_PREFIX + "/openapi.json"
        assert client.get(url).status_code == 401
        auth = {"Authorization": f"Bearer {owner.token}"}
        assert client.get(url, headers={**auth, "Host": "other.example"}).status_code == 403
        assert (
            client.get(url, headers={**auth, "Origin": "https://other.example"}).status_code == 403
        )
        origin = {"Origin": "https://ui.example"}
        preflight = client.options(
            url,
            headers={
                **origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin["Origin"]
        assert client.get(url, headers=origin).status_code == 401
        response = client.get(url, headers={**auth, **origin})
        assert response.status_code == 200
        schema = response.json()
        assert schema["security"] == [{"ControlToken": []}]
        status_schema = schema["paths"][API_PREFIX + "/status"]["get"]["responses"]["200"]
        assert "EngineStatus" in str(status_schema)
        from vcam.control_models import ConfigView, EngineStatus

        status = EngineStatus.model_validate(
            client.get(API_PREFIX + "/status", headers=auth).json()
        )
        config = ConfigView.model_validate(client.get(API_PREFIX + "/config", headers=auth).json())
        assert status.config.converged and status.config.revision == config.revision
        add = schema["paths"][API_PREFIX + "/cameras"]["post"]
        assert "CameraSpec" in str(add["requestBody"])
        assert "OperationResult" in str(add["responses"]["202"])
        assert (
            "text/event-stream"
            in schema["paths"][API_PREFIX + "/events"]["get"]["responses"]["200"]["content"]
        )


def test_strict_actions_validate_before_enqueue(owner: Controller) -> None:
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}"},
    ) as client:
        for data in ({"action": "stop"}, {"action": "stop", "allow_disruption": "false"}):
            response = client.post("engine/actions", json=data)
            assert response.status_code == (409 if len(data) == 1 else 422)
        assert client.post("engine/stop", data={"allow_disruption": "true"}).status_code == 415
        assert owner.supervisor.commands.empty()
        operation = client.post("engine/actions", json={"action": "stop", "allow_disruption": True})
        assert operation.status_code == 202
        assert operation.headers["location"].endswith(operation.json()["id"])
        assert not owner.supervisor.commands.empty()


def test_urls_include_disabled_only_when_requested_and_omit_credentials(owner: Controller) -> None:
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}", "If-Match": owner.expected_revision},
    ) as client:
        accepted = client.post(
            "cameras", json={"name": "later", "source": "later.mp4", "enabled": False}
        )
        assert accepted.status_code == 202
        owner.supervisor._drain_commands()
        assert client.get("urls").json()["urls"] == []
        listing = client.get("urls", params={"include_disabled": True, "host": "camera.example"})
        assert listing.json()["urls"] == ["rtsp://camera.example:8554/later"]
        assert "private-example" not in listing.text


def test_readiness_is_not_stream_health_and_reload_is_preview(owner: Controller) -> None:
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}"},
    ) as client:
        assert client.get("ready").json()["ready"] is True
        assert client.get("health").json()["ready"] is False
        config = client.get("config")
        assert "private-example" not in config.text
        assert "this comment" not in config.text
        assert "declared" in config.json() and "text" not in config.json()
        plan = client.post("config/reload")
        assert plan.status_code == 200
        assert plan.json()["base_revision"] == config.headers["etag"].strip('"')
        assert (
            client.post("config/apply", json={"plan_id": plan.json()["plan_id"]}).status_code == 428
        )
        assert owner.supervisor.commands.empty()


def test_queued_edit_rechecks_revision_before_persisting(owner: Controller) -> None:
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}"},
    ) as client:
        before = owner.config_path.read_bytes()
        headers = {"If-Match": owner.expected_revision}

        def submit(name):
            response = client.post(
                "cameras",
                headers=headers,
                json={"name": name, "source": "offline.mp4", "enabled": False},
            )
            assert response.status_code == 202
            return response.json()["id"]

        first, second = submit("one"), submit("two")
        assert owner.config_path.read_bytes() == before
        owner.supervisor._drain_commands()
        assert client.get(f"operations/{first}").json()["applied"] is True
        failed = client.get(f"operations/{second}").json()
        assert failed["error"]["code"] == "revision_mismatch"
        assert not failed["config_persisted"] and "two" not in owner.config_path.read_text()
        assert owner.status()["config"]["converged"]


def test_queue_full_and_runtime_failure_have_distinct_persistence_results(
    owner: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}"},
    ) as client:
        before = owner.config_path.read_bytes()
        headers = {"If-Match": owner.expected_revision, "Idempotency-Key": "retry-on-capacity"}
        data = {"name": "pending", "source": "later.mp4", "enabled": False}
        while not owner.supervisor.commands.full():
            owner.supervisor.commands.put_nowait(EngineCommand("stop"))
        assert client.post("cameras", headers=headers, json=data).status_code == 429
        assert owner.config_path.read_bytes() == before
        owner.supervisor._drain_commands()

        def fail(*args, **kwargs):
            raise ApplyError({"pending": "failed to start"})

        monkeypatch.setattr(owner.supervisor, "_apply", fail)
        op = client.post("cameras", headers=headers, json=data).json()
        owner.supervisor._drain_commands()
        failed = client.get(f"operations/{op['id']}").json()
        assert failed["config_persisted"] and not failed["applied"]
        assert failed["error"]["code"] == "apply_failed"
        assert "pending" in owner.config_path.read_text()
        assert not owner.status()["config"]["converged"]


def test_diagnostics_rejects_command_options_and_returns_media_fields(
    owner: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcam.probe import MediaInfo

    requested = []

    def probe(path, ffprobe, timeout):
        requested.append((path, ffprobe, timeout))
        return MediaInfo(path=path, codec="h264")

    monkeypatch.setattr("vcam.diagnostics.probe", probe)
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}"},
    ) as client:
        assert (
            client.post(
                "diagnostics/probe", json={"source": "clip.mp4", "ffprobe": "other"}
            ).status_code
            == 422
        )
        assert not requested
        (owner.config_path.parent / "clip.mp4").touch()
        result = client.post("diagnostics/probe", json={"source": "clip.mp4"})
        assert result.json()["codec"] == "h264" and "path" not in result.json()
        assert requested == [(owner.config_path.parent / "clip.mp4", "ffprobe", 20)]


def test_inline_crud_has_no_file_and_token_expires(
    owner: Controller, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    engine = Controller(
        owner.supervisor,
        None,
        effective=owner.effective,
        spec=owner.spec,
        revision=None,
        declared=owner.target_stack,
    )
    engine.publish_discovery("http://127.0.0.1:8765")
    try:
        before = engine.config()
        assert engine.token_file.is_file() and not list(engine.token_file.parent.glob("*.json"))
        assert before["persistence"] == "ephemeral" and before["path"] is None
        with TestClient(
            create_app(engine),
            base_url="http://localhost" + API_PREFIX,
            headers={"Authorization": f"Bearer {engine.token}"},
        ) as client:
            unready = client.post(
                "cameras",
                headers={"If-Match": before["revision"]},
                json={"name": "inline", "enabled": False, "source": "placeholder.mp4"},
            )
            assert client.get("ready").status_code == unready.status_code == 503
            assert unready.json()["error"]["code"] == "engine_starting"
            assert engine.supervisor.commands.empty()
            engine.tick(engine.supervisor)
            created = client.post(
                "cameras",
                headers={"If-Match": before["revision"]},
                json={"name": "inline", "enabled": False, "source": "placeholder.mp4"},
            )
            assert created.status_code == 202
            engine.supervisor._drain_commands()
            result = client.get("operations/" + created.json()["id"]).json()
            assert result["applied"] and not result["config_persisted"]
            after = client.get("config").json()
            assert after["disk_revision"] is None and after["revision"] != before["revision"]
            assert after["declared"]["cameras"][0]["name"] == "inline"
            assert client.post("config/reload").json()["error"]["code"] == "ephemeral_config"
    finally:
        engine.withdraw_discovery()
    assert not engine.token_file.exists() and "inline" not in owner.config_path.read_text()


@pytest.mark.skipif(os.getenv("VCAM_LIVE_TESTS") != "1", reason="opt-in real RTSP test")
def test_inline_control_process_streams_without_creating_yaml(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=96x96:r=12",
            "-t",
            "2",
            "-c:v",
            "libx264",
            str(clip),
        ],
        check=True,
        timeout=30,
    )
    with socket.socket() as media, socket.socket() as api:
        media.bind(("127.0.0.1", 0))
        api.bind(("127.0.0.1", 0))
        rtsp, control = media.getsockname()[1], api.getsockname()[1]
    root = tmp_path / "private"
    endpoint = f"http://127.0.0.1:{control}"
    with (tmp_path / "engine.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vcam",
                "run",
                "--source",
                str(clip),
                "--control",
                "--host",
                "127.0.0.1",
                "--port",
                str(rtsp),
                "--control-port",
                str(control),
                "--no-download",
                "--no-verify",
            ],
            cwd=tmp_path,
            env={**os.environ, "VCAM_STATE_DIR": str(root)},
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None:
                tokens = list((root / "control").glob("*.token"))
                if tokens:
                    client = ControlClient(endpoint, token=read_token_file(tokens[0]))
                    try:
                        if client.request("GET", "ready")["ready"]:
                            break
                    except ControlClientError as exc:
                        if exc.code != "unavailable" and exc.status != 503:
                            raise
                time.sleep(0.1)
            else:
                pytest.fail(
                    "inline control did not start: " + (tmp_path / "engine.log").read_text()
                )
            config = client.request("GET", "config")
            assert config["path"] is None and config["persistence"] == "ephemeral"
            op = client.request(
                "POST",
                "cameras",
                {"name": "second", "source": clip.name},
                revision=config["revision"],
            )
            finished = client.wait(op["id"])
            assert finished["applied"] and not finished["config_persisted"]
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    f"rtsp://127.0.0.1:{rtsp}/second",
                    "-frames:v",
                    "3",
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                timeout=20,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
        assert process.returncode == 0
    assert not list(tmp_path.glob("*.y*ml")) and not list((root / "control").glob("*.token"))


def test_conflicts_and_changed_media_do_not_persist(
    owner: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcam.probe import MediaInfo

    before = owner.config_path.read_bytes()
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}", "If-Match": owner.expected_revision},
    ) as client:
        candidate = {"name": "new", "source": "source.mp4"}
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            conflict = client.post("cameras", json={**candidate, "port": port})
            assert (
                conflict.status_code == 409
                and conflict.json()["error"]["code"] == "listener_unavailable"
            )
        candidate["port"] = port
        missing = client.post("cameras", json=candidate)
        assert missing.status_code == 422 and owner.supervisor.commands.empty()
        source = owner.config_path.parent / "source.mp4"
        source.touch()
        monkeypatch.setattr(
            "vcam.reconcile.probe", lambda path, **kw: MediaInfo(path=path, codec="h264")
        )
        operation = client.post("cameras", json=candidate).json()
        source.write_bytes(b"media replaced between admission and commit")
        owner.supervisor._drain_commands()
        failed = client.get("operations/" + operation["id"]).json()
        assert failed["error"]["code"] == "source_changed" and not failed["config_persisted"]
        assert owner.config_path.read_bytes() == before


def test_clock_changes_are_startup_only(owner: Controller) -> None:
    before = owner.config_path.read_text()
    proposal = before.replace("server: {", "server: {ntp_server: time.example, ")
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}", "If-Match": owner.expected_revision},
    ) as client:
        plan = client.post("config/plan", json={"text": proposal}).json()
        assert plan["restart_required"]
        rejected = client.post(
            "config/apply", json={"plan_id": plan["plan_id"], "allow_disruption": True}
        )
        assert (
            rejected.json()["error"]["code"] == "restart_required"
            and owner.supervisor.commands.empty()
        )
        assert owner.config_path.read_text() == before


def test_rename_keeps_transient_stop_and_disable_clears_it(owner: Controller) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    created = owner.create_camera(
        {"name": "before", "source": "rtsp://127.0.0.1/not-publishing", "port": port},
        if_match=owner.expected_revision,
        idempotency_key=None,
    )
    owner.supervisor._drain_commands()
    assert owner.operation(created["id"])["applied"]
    owner.supervisor.held_keys.add((port, "before"))
    for name, patch, held in (
        ("before", {"name": "after"}, {(port, "after")}),
        ("after", {"enabled": False}, set()),
    ):
        op = owner.update_camera(
            port, name, patch, if_match=owner.expected_revision, idempotency_key=None
        )
        owner.supervisor._drain_commands()
        assert owner.operation(op["id"])["applied"] and owner.supervisor.held_keys == held


def test_engine_start_failure_allows_repair_and_shutdown_cancels_queue(
    owner: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = owner.supervisor
    with monkeypatch.context() as patch:
        patch.setattr(
            runtime,
            "prepare",
            lambda admission=None: runtime.servers.append(ManagedProcess("fixture", [], "server")),
        )
        patch.setattr(ManagedProcess, "start", lambda self: False)
        failed = owner.engine_action("start", allow_disruption=False, idempotency_key=None)
        runtime._drain_commands()
        assert owner.operation(failed["id"])["status"] == "failed" and not runtime.stopping
    started = owner.engine_action("start", allow_disruption=False, idempotency_key=None)
    runtime._drain_commands()
    assert owner.operation(started["id"])["applied"] and runtime.engine_state == "running"
    queued = owner.engine_action("restart", allow_disruption=True, idempotency_key=None)
    runtime.shutdown()
    assert owner.operation(queued["id"])["status"] == "cancelled"


def test_completed_engine_operation_publishes_its_observed_state(owner: Controller) -> None:
    runtime = owner.supervisor
    runtime.on_tick = owner.tick
    for action, expected in (("start", "running"), ("stop", "stopped")):
        accepted = owner.engine_action(action, allow_disruption=True, idempotency_key=None)
        runtime._drain_commands()
        assert owner.operation(accepted["id"])["status"] == "succeeded"
        assert owner.status()["engine"]["state"] == expected


def test_real_listener_reports_schema_then_closes(owner: Controller) -> None:
    server = ControlServer(owner)
    try:
        server.start()
        client = ControlClient(server.endpoint, token=owner.token)
        assert client.request("GET", "ready")["ready"]
        assert "control" in client.request("GET", "schema/config")["properties"]
    finally:
        server.stop()
    assert not server.thread.is_alive()


@pytest.mark.parametrize(
    ("failure", "expected"), [(SupervisorError, typer.Exit), (KeyboardInterrupt, KeyboardInterrupt)]
)
def test_discovery_failure_releases_control_startup_resources(
    owner, monkeypatch, failure, expected
) -> None:
    from vcam.cli import _start_control_plane

    logger = logging.getLogger("vcam")
    handlers_before = logger.handlers.copy()
    stack = owner.supervisor.stack
    endpoint = None

    def fail_publish(self, url, ca_file):
        nonlocal endpoint
        endpoint = urlsplit(url)
        raise failure("discovery publication failed")

    monkeypatch.setattr(Controller, "publish_discovery", fail_publish)
    with pytest.raises(expected):
        _start_control_plane(
            owner.supervisor,
            owner.config_path,
            owner.expected_revision,
            stack,
            owner.effective,
            stack,
        )

    assert endpoint is not None
    with socket.socket() as listener, config_ownership(owner.config_path):
        listener.bind((endpoint.hostname, endpoint.port))
    assert logger.handlers == handlers_before
    assert owner.supervisor.on_tick is None


def test_symlinked_config_starts_and_edits_with_one_media_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcam.cli import _resolve_run_stack

    monkeypatch.setenv("VCAM_STATE_DIR", str(tmp_path / "state"))
    original, alias = tmp_path / "original", tmp_path / "alias"
    original.mkdir()
    alias.mkdir()
    (original / "clip.mp4").touch()
    target = original / "cameras.yaml"
    target.write_text(
        "control: {enabled: true, port: 0}\ncameras: [{name: cam, source: clip.mp4}]\n"
    )
    link = alias / "cameras.yaml"
    link.symlink_to(target)

    controlled, path, revision, declared = _resolve_run_stack(
        config=link, source=None, name=None, cameras=None, control=None
    )
    assert controlled and path is not None
    assert declared.cameras[0].source == target.resolve().parent / "clip.mp4"
    runtime = Supervisor(declared, None, work_dir=tmp_path / "runtime", allow_empty=True)
    controller = Controller(
        runtime,
        path,
        effective=lambda stack: stack,
        spec=declared.control,
        revision=revision,
        declared=declared,
    )
    controller.tick(runtime)
    plan = controller.plan(None, if_match=None)
    assert plan["updated"] == [] and not plan["disruptive"]


def test_patch_null_resets_defaulted_fields(owner: Controller) -> None:
    created = owner.create_camera(
        {
            "name": "cam",
            "source": "later.mp4",
            "enabled": False,
            "audio": True,
            "video": {"preset": "slow"},
            "simulation": {"mode": "flaky", "duration": 2},
        },
        if_match=owner.expected_revision,
        idempotency_key=None,
    )
    owner.supervisor._drain_commands()
    assert owner.operation(created["id"])["applied"]
    with TestClient(
        create_app(owner),
        base_url="http://localhost" + API_PREFIX,
        headers={"Authorization": f"Bearer {owner.token}"},
    ) as client:
        for patch in (
            {"audio": None, "video": {"preset": None}, "simulation": {"duration": None}},
            {"video": None, "simulation": None},
            {"video": {"fps": None}, "simulation": {"filters": None}},
        ):
            response = client.patch(
                "cameras/8554/cam", headers={"If-Match": owner.expected_revision}, json=patch
            )
            assert response.status_code == 202, response.text
            owner.supervisor._drain_commands()
            assert owner.operation(response.json()["id"])["applied"]
            text = owner.config_path.read_text()
            assert "null" not in text and "audio" not in text and "preset" not in text
            assert "duration" not in text
        assert set(owner.camera(8554, "cam")[0]["spec"]) == {"name", "source", "enabled"}


def test_events_cli_streams_json_through_a_pipe_before_disconnect(owner: Controller) -> None:
    server = ControlServer(owner)
    try:
        owner.claim_ownership()
        server.start()
        cursor = f"{owner.instance_id}:{owner.events.last_seq}"
        owner.events.publish("config", {"revision": "changed"})
        cmd = [
            sys.executable, "-m", "vcam", "events", "--json", "--since", cursor,
            "--endpoint", server.endpoint, "--token-file", str(owner.token_file),
        ]  # fmt: skip
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, env={**os.environ, "PYTHONUNBUFFERED": ""}
        ) as process:
            try:
                assert process.stdout is not None
                assert select.select([process.stdout], [], [], 10)[0], "no event on stdout"
                event = json.loads(process.stdout.readline())
                assert event["event"] == "config" and event["data"]["revision"] == "changed"
                assert event["id"] == f"{owner.instance_id}:{owner.events.last_seq}"
                assert process.poll() is None
            finally:
                process.terminate()
    finally:
        server.stop()
        owner.withdraw_discovery()


def test_slow_readiness_does_not_block_commands_or_mark_replaced_publisher_ready(
    owner, monkeypatch
):
    from vcam.control import _PathReadiness

    entered = threading.Event()
    release = threading.Event()

    def delayed(_self):
        entered.set()
        assert release.wait(2)
        return {"cam": {"ready": True}}

    monkeypatch.setattr("vcam.control.MediaMTXClient.list_paths", delayed)
    reader = _PathReadiness()
    old = ("http://127.0.0.1:9997", 12, (("old", 34),))
    new = (old[0], 12, (("new", 56),))
    try:
        assert reader.sample({8554: old}) == {8554: None}
        assert entered.wait(2)
        op = owner.engine_action("start", allow_disruption=False, idempotency_key=None)
        owner.supervisor._drain_commands()
        assert owner.operation(op["id"])["applied"]
        release.set()
        reader.pending[old].result(timeout=2)
        assert reader.sample({8554: new}) == {8554: None}
        reader.pending[new].result(timeout=2)
        assert reader.sample({8554: new}) == {8554: {"cam"}}
    finally:
        release.set()
        reader.close()


def test_identical_writes_share_one_operation_and_sse_can_resume(owner: Controller) -> None:
    server = ControlServer(owner)
    try:
        server.start()
        client = ControlClient(server.endpoint, token=owner.token)
        before = f"{owner.instance_id}:{owner.events.last_seq}"
        original = client.request("POST", "engine/start", idempotency_key="one-action")
        repeated = client.request("POST", "engine/start", idempotency_key="one-action")
        assert original["id"] == repeated["id"] and owner.supervisor.commands.qsize() == 1
        with pytest.raises(ControlClientError) as reused:
            client.request(
                "POST", "engine/stop", {"allow_disruption": True}, idempotency_key="one-action"
            )
        assert reused.value.code == "idempotency_key_reused"
        with contextlib.closing(client.events(before)) as stream:
            queued = next(stream)
        owner.supervisor._drain_commands()
        with contextlib.closing(client.events(queued.id)) as resumed:
            finished = next(resumed)
        assert finished.data["id"] == original["id"] and finished.data["status"] == "succeeded"
        with pytest.raises(ControlClientError) as invalid:
            next(client.events("different-instance:1"))
        assert invalid.value.code == "event_cursor_wrong_instance"
    finally:
        owner.supervisor.shutdown()
        server.stop()


def test_discovery_requires_exclusive_owner_and_token_survives_restart(owner: Controller) -> None:
    owner.publish_discovery("http://127.0.0.1:1234")
    first = read_descriptor(owner.config_path)
    assert first and first["instance_id"] == owner.instance_id
    token_file = Path(first["token_file"])
    original = read_token_file(token_file)
    assert owner.check_token(original)
    try:
        with (
            pytest.raises(SupervisorError, match="controls this config"),
            config_ownership(owner.config_path),
        ):
            pytest.fail("second process obtained ownership")
    finally:
        owner.withdraw_discovery()
    assert read_descriptor(owner.config_path) is None
    assert read_token_file(token_file) == original
    second = Controller(
        owner.supervisor,
        owner.config_path,
        effective=owner.effective,
        spec=owner.spec,
        revision=owner.expected_revision,
    )
    try:
        second.claim_ownership()
        assert second.instance_id != owner.instance_id and second.check_token(original)
    finally:
        second.withdraw_discovery()
    token_file.chmod(0o644)
    with pytest.raises(SupervisorError, match="owner-only"):
        read_token_file(token_file)


@pytest.mark.parametrize("password", ["private-password", "pa'private-password"])
def test_read_paths_and_logs_strip_query_credentials(owner: Controller, password: str) -> None:
    source = f"rtsp://reader:{password}@camera.example/feed?custom=private-query"
    document = owner.store.read()
    quoted = source.replace("'", "''")
    owner.store.commit(
        document.text.replace("cameras: []", f"cameras: [{{name: a, source: '{quoted}'}}]"),
        expected_revision=document.revision,
    )
    message = f"failed on {source}; Authorization: Bearer private-token"
    owner.logs.emit(logging.LogRecord("vcam", logging.WARNING, "", 1, message, (), None))
    assert "private-" not in str(owner.logs.tail(3))
    assert "private-query" not in str(owner.config())
    assert "private-password" not in str(owner.config())


def test_tls_control_verifies_the_selected_certificate(owner: Controller, tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl required for self-signed TLS fixture")
    cert, key = tmp_path / "local.crt", tmp_path / "local.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    owner.spec = owner.spec.model_copy(update={"tls_cert": cert, "tls_key": key})
    server = ControlServer(owner)
    try:
        server.start()
        with pytest.raises(ControlClientError):
            ControlClient(server.endpoint, token=owner.token).request("GET", "ready")
        trusted = ControlClient(
            server.endpoint,
            token=owner.token,
            ssl_context=ssl.create_default_context(cafile=str(cert)),
        )
        assert trusted.request("GET", "ready")["ready"]
    finally:
        server.stop()


@pytest.mark.parametrize(
    "settings",
    [
        {"allowed_origins": ["*"]},
        {"allowed_origins": ["https://ui.example/path"]},
        {"allowed_hosts": ["*.example"]},
    ],
)
def test_browser_policies_are_exact(settings) -> None:
    with pytest.raises(ValueError):
        ControlSpec.model_validate(settings)
