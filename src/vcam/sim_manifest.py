"""Convert the simulator's versioned stream manifest into a camera stack."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import format_validation_error
from .errors import ConfigError
from .models import CameraSpec, CameraStack, VideoCodec, VideoSettings
from .sources import is_live_source, parse_source


class SimCamera(BaseModel):
    id: str
    ingest_url: str
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    fps: float = Field(gt=0, le=240, strict=True)
    codec: Literal["h264", "hevc"] = "h264"

    @field_validator("ingest_url")
    @classmethod
    def _live_url(cls, value: str) -> str:
        if not is_live_source(parse_source(value)):
            raise ValueError("ingest_url must be a live rtsp:// or udp:// URL")
        return value


class SimManifest(BaseModel):
    schema_version: Literal["eais-sim-streams/1"] = Field(alias="schema")
    cameras: list[SimCamera] = Field(min_length=1)


def import_sim(path: Path) -> CameraStack:
    """Validate consumed fields; tolerate simulator metadata and future optional fields."""
    try:
        manifest = SimManifest.model_validate_json(path.read_text(encoding="utf-8"))
        return CameraStack(
            cameras=[
                CameraSpec(
                    name=camera.id,
                    source=camera.ingest_url,
                    video=VideoSettings(
                        resolution=f"{camera.width}x{camera.height}",
                        fps=camera.fps,
                        codec=VideoCodec.H265 if camera.codec == "hevc" else VideoCodec.H264,
                    ),
                )
                for camera in manifest.cameras
            ]
        )
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"could not read simulator manifest {path}: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{format_validation_error(exc)}") from exc
