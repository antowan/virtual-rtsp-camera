"""Tests for the Typer command line interface."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from vcam.cli import app
from vcam.config import load_stack

runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop Rich from wrapping output mid-token, which breaks substring assertions."""
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("TERM", "dumb")


def invoke(*args: str):
    return runner.invoke(app, list(args))


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


def test_help() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert "virtual RTSP cameras" in result.output


def test_version() -> None:
    """The reported version must be the real one, not merely present.

    This used to assert only that the word "vcam" appeared, so it stayed green
    while `__version__` sat two releases behind pyproject.toml.
    """
    from importlib.metadata import version as distribution_version

    result = invoke("--version")
    assert result.exit_code == 0
    assert result.output.strip() == f"vcam {distribution_version('vcam')}"


def test_the_packaged_version_is_the_one_in_pyproject() -> None:
    """Guards against going back to a hand-maintained copy of the version."""
    tomllib = pytest.importorskip("tomllib")  # 3.11+; the check is enough there

    from vcam import __version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert __version__ == declared


# ---------------------------------------------------------------------------
# init / add / list / urls
# ---------------------------------------------------------------------------


def test_init_writes_a_loadable_config(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    result = invoke("init", str(path))

    assert result.exit_code == 0
    assert path.is_file()
    payload = yaml.safe_load(path.read_text())
    assert len(payload["cameras"]) >= 2


def test_init_seeds_from_sources(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    result = invoke("init", str(path), "--source", str(video_file))

    assert result.exit_code == 0
    stack = load_stack(path)
    assert [camera.name for camera in stack.cameras] == ["clip"]


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text("cameras: []\n", encoding="utf-8")

    result = invoke("init", str(path))
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_add_appends_a_camera(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    invoke("init", str(path), "--source", str(video_file))

    result = invoke(
        "add", str(video_file), "--config", str(path), "--name", "extra", "--port", "8600"
    )
    assert result.exit_code == 0

    stack = load_stack(path)
    assert [camera.name for camera in stack.cameras] == ["clip", "extra"]
    assert stack.cameras[1].port == 8600


def test_add_rejects_duplicate_names(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    invoke("init", str(path), "--source", str(video_file))

    result = invoke("add", str(video_file), "--config", str(path), "--name", "clip")
    assert result.exit_code == 1
    assert "duplicate camera path" in result.output


def test_list_shows_urls(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    invoke("init", str(path), "--source", str(video_file))

    result = invoke("list", "--config", str(path))
    assert result.exit_code == 0
    assert "rtsp://127.0.0.1:8554/clip" in result.output


def test_urls_prints_one_per_line(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cameras": [
                    {"name": "a", "source": str(video_file)},
                    {"name": "b", "source": str(video_file), "port": 8555},
                    {"name": "c", "source": str(video_file), "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = invoke("urls", "--config", str(path))
    assert result.exit_code == 0
    assert result.output.split() == [
        "rtsp://127.0.0.1:8554/a",
        "rtsp://127.0.0.1:8555/b",
    ]


def test_urls_all_includes_disabled(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cameras": [
                    {"name": "a", "source": str(video_file)},
                    {"name": "c", "source": str(video_file), "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = invoke("urls", "--config", str(path), "--all")
    assert len(result.output.split()) == 2


def test_urls_uses_the_requested_host(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    invoke("init", str(path), "--source", str(video_file))

    result = invoke("urls", "--config", str(path), "--host", "10.0.0.5")
    assert result.output.strip() == "rtsp://10.0.0.5:8554/clip"


def test_missing_config_is_reported(tmp_path: Path) -> None:
    result = invoke("list", "--config", str(tmp_path / "nope.yaml"))
    assert result.exit_code == 1
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# run --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_emits_server_config_and_publishers(video_file: Path) -> None:
    result = invoke("run", "--camera", f"cam1={video_file}", "--dry-run")

    assert result.exit_code == 0
    assert "rtspAddress: ':8554'" in result.output or "rtspAddress: :8554" in result.output
    assert "rtsp://127.0.0.1:8554/cam1" in result.output
    assert "-stream_loop -1" in result.output


def test_dry_run_derives_names_from_bare_paths(video_file: Path) -> None:
    result = invoke("run", "--camera", str(video_file), "--dry-run")
    assert result.exit_code == 0
    assert "rtsp://127.0.0.1:8554/clip" in result.output


def test_dry_run_multiplexes_several_cameras(video_file: Path) -> None:
    result = invoke(
        "run",
        "--camera",
        f"a={video_file}",
        "--camera",
        f"b={video_file}",
        "--dry-run",
    )

    assert result.exit_code == 0
    assert "rtsp://127.0.0.1:8554/a" in result.output
    assert "rtsp://127.0.0.1:8554/b" in result.output
    assert result.output.count("mediamtx-8554.yml") == 1


def test_dry_run_honours_transcode_overrides(video_file: Path) -> None:
    result = invoke(
        "run",
        "-s",
        str(video_file),
        "-n",
        "cam",
        "--mode",
        "transcode",
        "--resolution",
        "640x360",
        "--fps",
        "10",
        "--bitrate",
        "600k",
        "--dry-run",
    )

    assert result.exit_code == 0
    assert "scale=640:360,fps=10" in result.output
    assert "-b:v 600k" in result.output


def test_dry_run_with_auth_embeds_credentials(video_file: Path) -> None:
    result = invoke(
        "run",
        "-s",
        str(video_file),
        "-n",
        "cam",
        "--username",
        "reader",
        "--password",
        "s3cr3t",
        "--dry-run",
    )

    assert result.exit_code == 0
    assert "rtsp://reader:s3cr3t@127.0.0.1:8554/cam" in result.output
    # The publisher itself must stay anonymous.
    assert "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/cam" in result.output


def test_password_without_username_is_rejected(video_file: Path) -> None:
    result = invoke("run", "-s", str(video_file), "--password", "s3cr3t", "--dry-run")
    assert result.exit_code == 1
    assert "must be provided together" in result.output


def test_invalid_password_characters_are_reported(video_file: Path) -> None:
    result = invoke("run", "-s", str(video_file), "-u", "reader", "-P", "bad pass", "--dry-run")
    assert result.exit_code == 1
    assert "MediaMTX" in result.output


def test_config_and_inline_cameras_are_mutually_exclusive(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    invoke("init", str(path), "--source", str(video_file))

    result = invoke("run", "--config", str(path), "-s", str(video_file), "--dry-run")
    assert result.exit_code == 1
    assert "not both" in result.output


def test_dry_run_overrides_apply_to_config_cameras(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cameras": [
                    {"name": "a", "source": str(video_file)},
                    {"name": "b", "source": str(video_file)},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = invoke("run", "-c", str(path), "--port", "9000", "--no-loop", "--dry-run")
    assert result.exit_code == 0
    assert "rtsp://127.0.0.1:9000/a" in result.output
    assert "rtsp://127.0.0.1:9000/b" in result.output
    assert "-stream_loop" not in result.output


def test_dry_run_plans_one_server_per_port(tmp_path: Path, video_file: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cameras": [
                    {"name": "a", "source": str(video_file)},
                    {"name": "b", "source": str(video_file), "port": 8600},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = invoke("run", "-c", str(path), "--dry-run")
    assert result.exit_code == 0
    assert "mediamtx-8554.yml" in result.output
    assert "mediamtx-8600.yml" in result.output


def test_bad_camera_argument_is_rejected(video_file: Path) -> None:
    result = invoke("run", "--camera", f"=%{video_file}", "--dry-run")
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# generate (interactive wizard)
# ---------------------------------------------------------------------------


def test_generate_writes_config_from_video_files(tmp_path: Path) -> None:
    """Wizard accepts all defaults and produces a loadable config."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)
    out = tmp_path / "cameras.yaml"

    # Input: include file (y), accept name (clip), accept mode (auto),
    #        accept offset (0), accept port (8554), no auth (n)
    result = runner.invoke(
        app,
        ["generate", str(out), "--scan", str(tmp_path)],
        input="y\nclip\nauto\n0\n8554\nn\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert out.is_file()
    stack = load_stack(out)
    assert len(stack.cameras) == 1
    assert stack.cameras[0].name == "clip"


def test_generate_skips_excluded_files(tmp_path: Path) -> None:
    """Files answered 'n' are not added to the config."""
    (tmp_path / "a.mp4").write_bytes(b"\x00" * 64)
    (tmp_path / "b.mp4").write_bytes(b"\x00" * 64)
    out = tmp_path / "cameras.yaml"

    # skip first file (n), include second (y), then defaults
    result = runner.invoke(
        app,
        ["generate", str(out), "--scan", str(tmp_path)],
        input="n\ny\nb\nauto\n0\n8554\nn\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    stack = load_stack(out)
    assert [c.name for c in stack.cameras] == ["b"]


def test_generate_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    out = tmp_path / "cameras.yaml"
    out.write_text("cameras: []\n", encoding="utf-8")

    result = runner.invoke(app, ["generate", str(out), "--scan", str(tmp_path)], input="")
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_generate_with_force_overwrites(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)
    out = tmp_path / "cameras.yaml"
    out.write_text("cameras: []\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["generate", str(out), "--scan", str(tmp_path), "--force"],
        input="y\nclip\nauto\n0\n8554\nn\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    stack = load_stack(out)
    assert stack.cameras[0].name == "clip"


def test_generate_exits_when_no_video_files_found(tmp_path: Path) -> None:
    out = tmp_path / "cameras.yaml"
    result = runner.invoke(app, ["generate", str(out), "--scan", str(tmp_path)], input="")
    assert result.exit_code != 0
    assert "No video files found" in result.output


def test_generate_sets_custom_rtsp_port(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)
    out = tmp_path / "cameras.yaml"

    result = runner.invoke(
        app,
        ["generate", str(out), "--scan", str(tmp_path)],
        input="y\nclip\nauto\n0\n9000\nn\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    stack = load_stack(out)
    assert stack.server.rtsp_port == 9000


def test_generate_with_auth(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)
    out = tmp_path / "cameras.yaml"

    result = runner.invoke(
        app,
        ["generate", str(out), "--scan", str(tmp_path)],
        input="y\nclip\nauto\n0\n8554\ny\nadmin\nsecret\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    stack = load_stack(out)
    assert stack.server.auth is not None
    assert stack.server.auth.username == "admin"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_toxiproxy_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # When neither cli nor api is available
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffmpeg" if "ff" in cmd else None)
    result = invoke("doctor")
    assert "toxiproxy: not detected" in result.output

    # When toxiproxy-cli is present
    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: "/usr/local/bin/toxiproxy-cli" if "toxi" in cmd else "/usr/bin/ffmpeg",
    )
    result = invoke("doctor")
    assert "OK   toxiproxy: cli: /usr/local/bin/toxiproxy-cli" in result.output

