"""Load and save camera stack configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import ConfigError
from .models import CameraSpec, CameraStack

DEFAULT_CONFIG_NAMES = ("cameras.yaml", "cameras.yml", "vcam.yaml", "vcam.yml")


def find_default_config(start: Path | None = None) -> Path | None:
    """Return the first well-known config file found in *start* (default: cwd)."""
    directory = start or Path.cwd()
    for name in DEFAULT_CONFIG_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _normalise_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Map the legacy ``streams:`` manifest onto the ``cameras:`` schema."""
    if "cameras" in data or "streams" not in data:
        return data

    converted: list[dict[str, Any]] = []
    for entry in data.get("streams") or []:
        if not isinstance(entry, dict):
            continue
        camera = {key: value for key, value in entry.items() if key != "offset_seconds"}
        if "offset_seconds" in entry:
            camera["start_offset"] = entry["offset_seconds"]
        converted.append(camera)

    normalised = {key: value for key, value in data.items() if key != "streams"}
    normalised["cameras"] = converted
    return normalised


def load_stack(path: Path, *, allow_empty: bool = False) -> CameraStack:
    """Parse a YAML config file into a validated :class:`CameraStack`.

    ``allow_empty`` accepts a stack with no cameras or replays; only the
    control API runtime does that, since it can add cameras later.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return parse_stack_text(path.read_text(encoding="utf-8"), path, allow_empty=allow_empty)


def parse_stack_text(text: str, path: Path | None, *, allow_empty: bool = False) -> CameraStack:
    """Resolve sources beside the config, or in the launch directory for an inline stack."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(raw).__name__}")

    try:
        stack = CameraStack.model_validate(_normalise_legacy(raw))
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{format_validation_error(exc)}") from exc

    if not allow_empty and not stack.cameras and not stack.replays:
        raise ConfigError(f"{path} declares no cameras or replays")

    return _resolve_sources(stack, path.parent if path is not None else Path.cwd())


def _resolve_sources(stack: CameraStack, base_dir: Path) -> CameraStack:
    """Resolve relative source paths against the config file's directory."""
    for setting in ("tls_cert", "tls_key", "token_file"):
        value = getattr(stack.control, setting)
        if value is not None and not value.is_absolute():
            setattr(stack.control, setting, base_dir.absolute() / value)
    for camera in stack.cameras:
        if isinstance(camera.source, Path) and not camera.source.is_absolute():
            camera.source = (base_dir / camera.source).resolve()
    for replay in stack.replays:
        if not replay.source.is_absolute():
            replay.source = (base_dir / replay.source).resolve()
        if replay.sdp is not None and not replay.sdp.is_absolute():
            replay.sdp = (base_dir / replay.sdp).resolve()
    return stack


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  - {location}: {message}")
    return "\n".join(lines)


def dump_stack(stack: CameraStack) -> str:
    """Serialise a stack back to YAML, omitting defaults for readability."""
    payload = stack.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    payload.setdefault("server", {})
    if not payload.get("control"):
        payload.pop("control", None)
    payload["cameras"] = [
        camera.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
        | {"name": camera.name, "source": str(camera.source)}
        for camera in stack.cameras
    ]
    if stack.replays:
        payload["replays"] = [
            replay.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
            | {"name": replay.name, "source": str(replay.source), "port": replay.port}
            for replay in stack.replays
        ]
    else:
        payload.pop("replays", None)
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)


def save_stack(stack: CameraStack, path: Path) -> None:
    """Atomically replace ``path`` with ``stack``, failing if it changed meanwhile."""
    from .config_store import ConfigStore

    store = ConfigStore(path)
    expected = store.read().revision if store.path.exists() else None
    store.commit(dump_stack(stack), expected_revision=expected)


def example_stack(sources: list[Path] | None = None) -> CameraStack:
    """Build a documented starter configuration."""
    if sources:
        cameras = [
            CameraSpec(name=_slugify(source.stem) or f"cam{index + 1}", source=source)
            for index, source in enumerate(sources)
        ]
    else:
        cameras = [
            CameraSpec(name="cam1", source=Path("videos/cam1.mp4")),
            CameraSpec(
                name="cam2",
                source=Path("videos/cam2.mp4"),
                start_offset=12,
                mode="transcode",  # type: ignore[arg-type]
                video={"resolution": "1280x720", "fps": 15, "bitrate": "2M"},  # type: ignore[arg-type]
            ),
        ]
    return CameraStack(cameras=cameras)


def _slugify(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_-." else "-" for char in value)
    return cleaned.strip("-.") or ""
