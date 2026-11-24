"""Classify the difference between the running stack and a desired one.

A camera is identified by its effective RTSP port and name, so the same name
on two ports is two cameras, and moving a camera to another port is a removal
plus an addition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SupervisorError
from .mediamtx import _port_is_free
from .models import CameraSpec, CameraStack
from .probe import MediaInfo, probe
from .synchronized import CopySource

CameraKey = tuple[int, str]


def camera_key(stack: CameraStack, camera: CameraSpec) -> CameraKey:
    return (stack.effective_port(camera), camera.name)


def enabled_by_key(stack: CameraStack) -> dict[CameraKey, CameraSpec]:
    return {camera_key(stack, camera): camera for camera in stack.enabled_cameras}


@dataclass(frozen=True)
class ChangePlan:
    """What applying a desired stack does to the running one.

    Granular changes (``added``/``removed``/``updated`` independent cameras)
    leave every other publisher, server and reader connection alone.
    ``disruptive_reasons`` lists changes that need a full engine restart;
    ``restart_required`` lists settings only a new ``vcam run`` picks up.
    """

    added: tuple[CameraKey, ...] = ()
    removed: tuple[CameraKey, ...] = ()
    updated: tuple[CameraKey, ...] = ()
    disruptive_reasons: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()

    @property
    def disruptive(self) -> bool:
        return bool(self.disruptive_reasons)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.updated or self.disruptive_reasons)

    def to_dict(self) -> dict[str, Any]:
        def refs(keys: tuple[CameraKey, ...]) -> list[dict[str, Any]]:
            return [{"port": port, "name": name} for port, name in keys]

        return {
            "added": refs(self.added),
            "removed": refs(self.removed),
            "updated": refs(self.updated),
            "disruptive": self.disruptive,
            "disruptive_reasons": list(self.disruptive_reasons),
            "restart_required": list(self.restart_required),
        }


def file_stamp(path: Path) -> tuple[int, int, int, int]:
    if not path.is_file():
        raise SupervisorError("media source must be a readable regular file")
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def check_listeners(target: CameraStack, owned: set[int], control_port: int | None) -> None:
    ports = {target.effective_port(c): target.server.host for c in target.enabled_cameras}
    ports.update({r.port: target.server.host for r in target.enabled_replays})
    if target.ingest:
        ports[target.ingest.rtsp_port] = target.ingest.host
    for port, host in ports.items():
        # On BSD, probing a wildcard alone can miss an existing loopback bind.
        # Our local publishers must also be able to reach this listener on loopback.
        hosts: tuple[str, ...] = (host, "127.0.0.1") if host in {"", "0.0.0.0"} else (host,)
        if host == "::":
            hosts = (host, "::1", "127.0.0.1")
        if port == control_port or (
            port not in owned and not all(_port_is_free(port, host=h) for h in hosts)
        ):
            raise SupervisorError(f"RTSP listener port {port} is unavailable")


@dataclass
class MediaAdmission:
    stamps: dict[Path, tuple[int, int, int, int]] = field(default_factory=dict)
    media: dict[Path, MediaInfo] = field(default_factory=dict)
    synchronized: dict[Path, CopySource] = field(default_factory=dict)


def preflight(target: CameraStack, plan: ChangePlan, ffprobe: str) -> MediaAdmission:
    """Admit changed media before committing or interrupting a running publisher."""
    from .replay_source import load
    from .synchronized import admit_source, compatible_sources

    changed = set(plan.added + plan.updated)
    groups: dict[str, list[CopySource]] = {}
    admission = MediaAdmission()
    for camera in target.enabled_cameras:
        if not plan.disruptive and camera_key(target, camera) not in changed:
            continue
        if not isinstance(camera.source, Path):
            continue  # An offline live source can be admitted and retried later.
        path = camera.source
        if path not in admission.stamps:
            admission.stamps[path] = file_stamp(path)
            info = probe(path, ffprobe=ffprobe, timeout=20)
            if not info.codec:
                raise SupervisorError("camera source has no video track")
            admission.media[path] = info
        if camera.sync_group:
            if path not in admission.synchronized:
                admission.synchronized[path] = admit_source(path)
            groups.setdefault(camera.sync_group, []).append(admission.synchronized[path])
    for sources in groups.values():
        compatible_sources(sources)
    if plan.disruptive:
        for replay in target.enabled_replays:
            if replay.source not in admission.stamps:
                admission.stamps[replay.source] = file_stamp(replay.source)
            if replay.sdp and replay.sdp not in admission.stamps:
                admission.stamps[replay.sdp] = file_stamp(replay.sdp)
            load(replay.source, sdp_override=replay.sdp)
    return admission


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json") if model is not None else None


def plan_changes(current: CameraStack, target: CameraStack) -> ChangePlan:
    reasons: list[str] = []
    restart: list[str] = []
    if _dump(current.server) != _dump(target.server):
        reasons.append("server settings changed")
    if current.server.ntp_server != target.server.ntp_server:
        restart.append("clock configuration takes effect on the next `vcam run`")
    if _dump(current.ingest) != _dump(target.ingest):
        reasons.append("ingest settings changed")
    if [_dump(r) for r in current.enabled_replays] != [_dump(r) for r in target.enabled_replays]:
        reasons.append("replays changed")
    if _dump(current.control) != _dump(target.control):
        restart.append("control settings take effect on the next `vcam run`")

    before = enabled_by_key(current)
    after = enabled_by_key(target)
    added = tuple(key for key in after if key not in before)
    removed = tuple(key for key in before if key not in after)
    updated = tuple(
        key for key in after if key in before and _dump(before[key]) != _dump(after[key])
    )
    groups = sorted(
        {
            camera.sync_group
            for key in (*added, *removed, *updated)
            for camera in (before.get(key), after.get(key))
            if camera is not None and camera.sync_group is not None
        }
    )
    if groups:
        reasons.append("synchronized cameras changed (sync_group " + ", ".join(groups) + ")")
    return ChangePlan(added, removed, updated, tuple(reasons), tuple(restart))
