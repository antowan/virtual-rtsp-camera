from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import json
import stat
import threading
from pathlib import Path

import pytest

from vcam import config_store
from vcam.config_store import (
    ConfigMigrationRequired,
    ConfigRevisionConflict,
    ConfigStore,
    ConfigWriteError,
)
from vcam.errors import ConfigError


def test_read_preview_commit_preserves_human_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    original = (
        b"# My clips\r\n"
        b"cameras:\r\n"
        b"  - name: lobby\r\n"
        b"    source: 'clips/lobby.mp4'  # relative to this file\r\n"
        b"    enabled: true\r\n"
        b"replays: []  # reserved\r\n"
    )
    path.write_bytes(original)
    path.chmod(0o640)
    store = ConfigStore(path)
    document = store.read()
    assert document.revision == hashlib.sha256(original).hexdigest()
    document.data["cameras"][0]["enabled"] = False
    preview = store.preview(document)
    assert path.read_bytes() == original
    saved = store.commit(preview, expected_revision=document.revision)
    assert path.read_bytes() == original.replace(b"enabled: true", b"enabled: false")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert store.read().revision == saved.revision != document.revision


def test_external_comment_edit_is_a_conflict(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    store = ConfigStore(path)
    first = store.commit("cameras: []\n", expected_revision=None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(b"# changed outside VCAM\ncameras: []\n")
    with pytest.raises(ConfigRevisionConflict) as caught:
        store.commit("replays: []\n", expected_revision=first.revision)
    assert caught.value.expected_revision == first.revision
    assert caught.value.actual_revision == store.read().revision
    assert path.read_bytes().startswith(b"# changed outside VCAM\n")
    with pytest.raises(ConfigRevisionConflict):
        store.commit(first.text, expected_revision=None)


def test_concurrent_edit_has_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    store = ConfigStore(path)
    initial = store.commit("cameras: []\n", expected_revision=None)
    barrier = threading.Barrier(2, timeout=3)

    def edit(label: str) -> str:
        barrier.wait()
        try:
            return store.commit(
                f"# {label}\ncameras: []\n", expected_revision=initial.revision
            ).text
        except (ConfigRevisionConflict, ConfigWriteError):
            return ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, ["A", "B"]))
    assert sorted(results) == ["", path.read_text()]
    assert path.read_text().startswith("# ")


def test_aliases_allowed_for_reads_and_full_edits_not_granular_edits(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text("cameras: [ &camera {name: a, source: x}, *camera ]\n")
    store = ConfigStore(path)
    document = store.read()
    document.data["cameras"][0]["source"] = "new"
    with pytest.raises(ConfigMigrationRequired):
        store.preview(document)
    assert path.read_text() == document.text
    store.commit("cameras: []\n", expected_revision=document.revision)
    assert path.read_text() == "cameras: []\n"


def test_invalid_document_and_readonly_config_leave_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    store = ConfigStore(path)
    initial = store.commit("cameras: []\n", expected_revision=None)
    with pytest.raises(ConfigError, match="invalid YAML") as caught:
        store.commit("[secret-sentinel", expected_revision=initial.revision)
    assert "secret-sentinel" not in str(caught.value)
    path.chmod(0o444)
    with pytest.raises(ConfigWriteError, match="read-only") as readonly:
        store.commit("# new\ncameras: []\n", expected_revision=initial.revision)
    assert not readonly.value.config_persisted
    assert path.read_text() == initial.text


def test_lock_symlink_and_failed_replace_do_not_change_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text("cameras: []\n")
    store = ConfigStore(path)
    initial = store.read()
    lock = tmp_path / ".cameras.yaml.vcam-lock"
    other = tmp_path / "other"
    other.write_bytes(b"do not touch")
    lock.symlink_to(other)
    with pytest.raises(ConfigWriteError):
        store.commit("# new\ncameras: []\n", expected_revision=initial.revision)
    assert other.read_bytes() == b"do not touch"
    lock.unlink()

    def fail_replace(src: Path, dst: Path) -> None:
        raise OSError(errno.EBUSY, "device or resource busy")

    monkeypatch.setattr(config_store.os, "replace", fail_replace)
    with pytest.raises(ConfigWriteError, match="busy") as failure:
        store.commit("# new\ncameras: []\n", expected_revision=initial.revision)
    assert not failure.value.config_persisted
    assert path.read_text() == initial.text
    assert set(tmp_path.iterdir()) == {path, lock, other}


def test_error_after_replace_reports_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cameras.yaml"
    store = ConfigStore(path)

    def fail_sync(directory: Path) -> None:
        raise OSError(errno.EIO, "disk failure")

    monkeypatch.setattr(config_store, "_sync_directory", fail_sync)
    with pytest.raises(ConfigWriteError) as failure:
        store.commit("cameras: []\n", expected_revision=None)
    assert failure.value.config_persisted
    assert path.read_text() == "cameras: []\n"


def test_offline_cli_preserves_legacy_document_and_respects_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from vcam.cli import app
    from vcam.config import load_stack
    from vcam.control import config_ownership

    monkeypatch.setenv("VCAM_STATE_DIR", str(tmp_path / "private"))
    path = tmp_path / "legacy.yaml"
    path.write_text("# user note\nstreams:\n  - name: original\n    source: 'relative.mp4'\n")
    runner = CliRunner()
    args = [
        "add",
        "clip.mp4",
        "--name",
        "second",
        "--start-offset",
        "2",
        "-c",
        str(path),
        "--offline",
    ]
    original = path.read_bytes()
    refused = runner.invoke(app, [*args, "--no-wait"])
    assert refused.exit_code != 0 and "--no-wait" in refused.output
    assert path.read_bytes() == original
    saved = runner.invoke(app, [*args, "--json"])
    assert saved.exit_code == 0, saved.output
    result = json.loads(saved.stdout)
    assert result["config_persisted"] and not result["applied"]
    assert result["revision"] == ConfigStore(path).disk_revision()
    text = path.read_text()
    assert "# user note" in text and "source: 'relative.mp4'" in text and "cameras:" not in text
    assert "offset_seconds: 2" in text and load_stack(path).cameras[1].start_offset == 2
    with config_ownership(path):
        for argv in (args, ["init", str(path), "--force"]):
            refused = runner.invoke(app, argv)
            assert refused.exit_code != 0 and "owns" in refused.output
            assert path.read_text() == text
