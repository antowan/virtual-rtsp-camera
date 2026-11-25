"""Tests for the service management module and its CLI commands."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vcam import service as svc
from vcam.cli import app
from vcam.service import ServiceError, ServiceStatus

runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("TERM", "dumb")


def _ok(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr="")


def _err(stderr: str = "", returncode: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# detect_backend
# ---------------------------------------------------------------------------


def test_detect_backend_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Linux")
    assert svc.detect_backend() == "systemd"


def test_detect_backend_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Darwin")
    assert svc.detect_backend() == "launchd"


def test_detect_backend_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Windows")
    assert svc.detect_backend() is None


# ---------------------------------------------------------------------------
# vcam_command
# ---------------------------------------------------------------------------


def test_vcam_command_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.service.shutil.which", lambda _: "/usr/local/bin/vcam")
    assert svc.vcam_command() == ["/usr/local/bin/vcam"]


def test_vcam_command_falls_back_to_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vcam.service.shutil.which", lambda _: None)
    import sys

    cmd = svc.vcam_command()
    assert cmd[0] == sys.executable
    assert cmd[1:] == ["-m", "vcam"]


# ---------------------------------------------------------------------------
# render_systemd_unit
# ---------------------------------------------------------------------------


def test_render_systemd_unit_structure(tmp_path: Path) -> None:
    config = tmp_path / "cameras.yaml"
    config.touch()
    unit = svc.render_systemd_unit("vcam", ["/usr/local/bin/vcam"], config)

    assert "[Unit]" in unit
    assert "[Service]" in unit
    assert "[Install]" in unit
    assert f"ExecStart=/usr/local/bin/vcam run -c {config}" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    assert f"WorkingDirectory={config.parent}" in unit


def test_render_systemd_unit_quotes_paths_with_spaces(tmp_path: Path) -> None:
    config = tmp_path / "my cameras" / "cameras.yaml"
    config.parent.mkdir()
    config.touch()
    unit = svc.render_systemd_unit("vcam", ["/usr/local/bin/vcam"], config)
    # The config path contains a space, so it must be quoted in ExecStart.
    assert f'"run" "-c" "{config}"' in unit or f'run -c "{config}"' in unit


# ---------------------------------------------------------------------------
# render_launchd_plist
# ---------------------------------------------------------------------------


def test_render_launchd_plist_structure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "cameras.yaml"
    config.touch()

    raw = svc.render_launchd_plist("vcam", ["/usr/local/bin/vcam"], config)
    payload = plistlib.loads(raw)

    assert payload["Label"] == "vcam"
    assert payload["ProgramArguments"] == ["/usr/local/bin/vcam", "run", "-c", str(config)]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert str(config.parent) == payload["WorkingDirectory"]
    assert "StandardOutPath" in payload
    assert "vcam" in payload["StandardOutPath"]


# ---------------------------------------------------------------------------
# check_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["vcam", "my-service", "vcam.main", "vcam_01"])
def test_check_name_valid(name: str) -> None:
    svc.check_name(name)  # should not raise


@pytest.mark.parametrize("name", ["", "-bad", "bad name", "bad/name"])
def test_check_name_invalid(name: str) -> None:
    with pytest.raises(ServiceError, match="invalid service name"):
        svc.check_name(name)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_systemd_unit_path_respects_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert svc.systemd_unit_path("vcam") == tmp_path / "systemd" / "user" / "vcam.service"


def test_systemd_unit_path_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        svc.systemd_unit_path("vcam") == tmp_path / ".config" / "systemd" / "user" / "vcam.service"
    )


def test_launchd_paths_use_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert svc.launchd_plist_path("vcam") == tmp_path / "Library" / "LaunchAgents" / "vcam.plist"
    assert svc.launchd_log_path("vcam") == tmp_path / "Library" / "Logs" / "vcam-vcam.log"


# ---------------------------------------------------------------------------
# install — systemd
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_systemd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up a fake Linux/systemd environment."""
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr("vcam.service.shutil.which", lambda _: "/usr/local/bin/vcam")
    calls: list[list[str]] = []

    def fake_run(argv, *, check=True, capture=True):
        calls.append(list(argv))
        return _ok()

    monkeypatch.setattr("vcam.service._run", fake_run)
    return tmp_path, calls


def test_install_systemd_writes_unit_and_calls_systemctl(
    fake_systemd: Any,
    tmp_path: Path,
) -> None:
    base, calls = fake_systemd
    config = tmp_path / "cameras.yaml"
    config.write_text("cameras: [{name: cam1, source: v.mp4}]", encoding="utf-8")

    svc.install("vcam", config)

    unit_path = base / "cfg" / "systemd" / "user" / "vcam.service"
    assert unit_path.is_file()
    unit_text = unit_path.read_text(encoding="utf-8")
    assert str(config.resolve()) in unit_text
    assert "Restart=always" in unit_text

    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "vcam.service"] in calls


def test_install_systemd_missing_config_raises(fake_systemd: Any, tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="not found"):
        svc.install("vcam", tmp_path / "missing.yaml")


# ---------------------------------------------------------------------------
# install — launchd
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_launchd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up a fake macOS/launchd environment."""
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("vcam.service.shutil.which", lambda _: "/usr/local/bin/vcam")
    monkeypatch.setattr("vcam.service.os.getuid", lambda: 501)
    calls: list[list[str]] = []

    def fake_run(argv, *, check=True, capture=True):
        calls.append(list(argv))
        if argv[0] == "launchctl" and argv[1] == "bootstrap" and check:
            return _ok()
        return _ok()

    monkeypatch.setattr("vcam.service._run", fake_run)
    return tmp_path, calls


def test_install_launchd_writes_plist_and_loads(
    fake_launchd: Any,
    tmp_path: Path,
) -> None:
    base, calls = fake_launchd
    config = tmp_path / "cameras.yaml"
    config.write_text("cameras: [{name: cam1, source: v.mp4}]", encoding="utf-8")

    svc.install("vcam", config)

    plist_path = base / "Library" / "LaunchAgents" / "vcam.plist"
    assert plist_path.is_file()
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == "vcam"
    assert str(config.resolve()) in payload["ProgramArguments"]

    # bootstrap or load should have been called
    assert any((a[0] == "launchctl" and "bootstrap" in a) or "load" in a for a in calls)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_systemd_active(fake_systemd: Any) -> None:
    base, _calls = fake_systemd
    # Create the unit file so it's "installed"
    unit_path = base / "cfg" / "systemd" / "user" / "vcam.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text("[Service]\nExecStart=vcam run\n", encoding="utf-8")

    responses = iter([_ok("active\n"), _ok("enabled\n")])

    import vcam.service as svc_mod

    svc_mod._run = lambda argv, *, check=True, capture=True: next(responses)  # type: ignore

    result = svc.status("vcam")
    assert result.active is True
    assert result.installed is True
    assert "active" in result.line


def test_status_systemd_not_installed(fake_systemd: Any) -> None:
    result = svc.status("vcam")
    assert result.active is False
    assert result.installed is False
    assert "not installed" in result.line


def test_status_launchd_running(fake_launchd: Any, tmp_path: Path) -> None:
    base, _calls = fake_launchd
    plist = base / "Library" / "LaunchAgents" / "vcam.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"<plist/>")

    import vcam.service as svc_mod

    svc_mod._run = lambda argv, *, check=True, capture=True: _ok("1234\t0\tvcam\n")  # type: ignore

    result = svc.status("vcam")
    assert result.active is True
    assert "1234" in result.line


def test_status_launchd_not_installed(fake_launchd: Any) -> None:
    result = svc.status("vcam")
    assert result.active is False
    assert result.installed is False


# ---------------------------------------------------------------------------
# stop / uninstall
# ---------------------------------------------------------------------------


def test_stop_systemd_calls_systemctl(fake_systemd: Any, tmp_path: Path) -> None:
    base, calls = fake_systemd
    unit = base / "cfg" / "systemd" / "user" / "vcam.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n", encoding="utf-8")

    svc.stop("vcam")
    assert ["systemctl", "--user", "stop", "vcam.service"] in calls


def test_stop_systemd_not_installed_raises(
    fake_systemd: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unit file doesn't exist; make systemctl stop fail so the path-check runs.
    import vcam.service as svc_mod

    svc_mod._run = lambda argv, *, check=True, capture=True: _err("not loaded", returncode=5)  # type: ignore
    with pytest.raises(ServiceError, match="not installed"):
        svc.stop("vcam")


def test_uninstall_systemd(fake_systemd: Any, tmp_path: Path) -> None:
    base, calls = fake_systemd
    unit = base / "cfg" / "systemd" / "user" / "vcam.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n", encoding="utf-8")

    svc.uninstall("vcam")

    assert not unit.exists()
    assert any("disable" in a for a in calls)


def test_uninstall_not_installed_raises(fake_systemd: Any) -> None:
    with pytest.raises(ServiceError, match="not installed"):
        svc.uninstall("vcam")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def invoke(*args: str):
    return runner.invoke(app, list(args))


def test_service_help() -> None:
    result = invoke("service", "--help")
    assert result.exit_code == 0
    for cmd in ("install", "start", "stop", "status", "uninstall", "logs"):
        assert cmd in result.output


def test_service_install_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no cameras.yaml in cwd
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Linux")
    result = invoke("service", "install")
    assert result.exit_code == 1
    assert "vcam init" in result.output


def test_service_install_bad_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_systemd: Any,
) -> None:
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("cameras: [{name: 'bad name!', source: v.mp4}]", encoding="utf-8")
    result = invoke("service", "install", "--config", str(bad_config))
    assert result.exit_code == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_empty_service_needs_control(
    enabled: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_systemd: Any
) -> None:
    import importlib.util

    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name in {"fastapi", "uvicorn"} else original(name),
    )
    config = tmp_path / "cameras.yaml"
    config.write_text(f"control: {{enabled: {str(enabled).lower()}}}\ncameras: []\n")
    result = invoke("service", "install", "-c", str(config))
    base, calls = fake_systemd
    unit = base / "cfg" / "systemd" / "user" / "vcam.service"
    if enabled:
        assert result.exit_code == 0, result.output
        assert f"ExecStart=/usr/local/bin/vcam run -c {config}\n" in unit.read_text()
    else:
        assert result.exit_code != 0
        assert "declares no cameras or replays" in result.output
        assert not unit.exists() and not calls


def test_service_missing_control_extra_does_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_systemd: Any
) -> None:
    import importlib.util

    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None if name == "uvicorn" else original(name)
    )
    config = tmp_path / "cameras.yaml"
    config.write_text("control: {enabled: true}\ncameras: []\n")
    result = invoke("service", "install", "-c", str(config))
    assert result.exit_code != 0
    assert "install VCAM with the control extra" in result.output
    assert not fake_systemd[1]


def test_service_install_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, video_file: Path
) -> None:
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Windows")
    config = tmp_path / "cameras.yaml"
    config.write_text(f"cameras: [{{name: cam1, source: {video_file}}}]", encoding="utf-8")
    result = invoke("service", "install", "--config", str(config))
    assert result.exit_code == 1
    assert "not supported" in result.output


def test_service_status_cli_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vcam.service.status",
        lambda name: ServiceStatus("vcam.service: not installed", active=False, installed=False),
    )
    result = invoke("service", "status")
    assert result.exit_code == 3
    assert "not installed" in result.output


def test_service_status_cli_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vcam.service.status",
        lambda name: ServiceStatus("vcam.service: active, enabled", active=True, installed=True),
    )
    result = invoke("service", "status")
    assert result.exit_code == 0
    assert "active" in result.output


def test_service_stop_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = []
    monkeypatch.setattr(
        "vcam.service.stop", lambda name: stopped.append(name) or "stopped vcam.service"
    )
    result = invoke("service", "stop")
    assert result.exit_code == 0
    assert stopped == ["vcam"]


def test_service_uninstall_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    removed = []
    monkeypatch.setattr(
        "vcam.service.uninstall", lambda name: removed.append(name) or "removed /path"
    )
    result = invoke("service", "uninstall")
    assert result.exit_code == 0
    assert removed == ["vcam"]


def test_service_install_cli_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    video_file: Path,
) -> None:
    monkeypatch.setattr("vcam.service.platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr("vcam.service.shutil.which", lambda _: "/usr/local/bin/vcam")
    monkeypatch.setattr("vcam.service._run", lambda argv, **kw: _ok())

    config = tmp_path / "cameras.yaml"
    config.write_text(f"cameras: [{{name: cam1, source: {video_file}}}]", encoding="utf-8")
    result = invoke("service", "install", "--config", str(config))
    assert result.exit_code == 0
    assert "installed" in result.output
