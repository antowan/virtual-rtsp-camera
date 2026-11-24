"""Versioned control wire types. Importing them does not require the server extra."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, with_config
from typing_extensions import TypedDict

from .models import (
    CAMERA_NAME_RE,
    SimulationMode,
    StreamMode,
    Transport,
    VideoCodec,
)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CameraRef(RequestBody):
    port: int = Field(ge=1, le=65535)
    name: str = Field(pattern=CAMERA_NAME_RE.pattern)


@with_config(ConfigDict(extra="forbid"))
class VideoPatch(TypedDict, total=False):
    codec: VideoCodec | None
    encoder: str | None
    resolution: str | None
    fps: float | None
    bitrate: str | None
    gop: int | None
    preset: str | None


@with_config(ConfigDict(extra="forbid"))
class SimulationPatch(TypedDict, total=False):
    mode: SimulationMode | None
    noise_level: int | None
    interval: float | None
    duration: float | None
    filters: str | None


@with_config(ConfigDict(extra="forbid"))
class CameraPatch(TypedDict, total=False):
    """RFC 7396 merge patch: ``null`` resets a field to its default.

    Only supplied keys are merged; the resulting CameraSpec is validated whole.
    """

    name: str
    source: str
    source_timeout: float | None
    enabled: bool | None
    loop: bool | None
    realtime: bool | None
    mode: StreamMode | None
    start_offset: float | None
    transport: Transport | None
    audio: bool | None
    port: int | None
    video: VideoPatch | None
    simulation: SimulationPatch | None
    sync_group: str | None


class Action(RequestBody):
    action: Literal["start", "stop", "restart"]


class Confirmation(RequestBody):
    allow_disruption: StrictBool = False


class EngineAction(Action, Confirmation):
    pass


class PlanRequest(RequestBody):
    text: str | None = None


class ApplyRequest(Confirmation):
    plan_id: str = Field(min_length=1, max_length=64)


class ReloadRequest(RequestBody):
    preview: StrictBool = True


class ProbeSource(RequestBody):
    source: str = Field(min_length=1)


class ProbeCamera(RequestBody):
    camera: CameraRef


class ErrorDetails(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RequestError(ErrorDetails):
    request_id: str


class ErrorEnvelope(BaseModel):
    error: RequestError


EnginePhase = Literal["starting", "running", "stopping", "stopped", "failed"]


class EngineState(BaseModel):
    state: EnginePhase


class ReadyView(BaseModel):
    ready: bool
    engine: EnginePhase


class ConfigRevision(BaseModel):
    path: str | None
    persistence: Literal["file", "read-only", "ephemeral"]
    disk_revision: str | None
    revision: str | None
    expected_revision: str
    applied_revision: str | None
    pending_reload: bool


class ConfigStatus(ConfigRevision):
    converged: bool


class ConfigProvenance(BaseModel):
    declared: Literal["file", "inline"]
    overridden: list[str]


class ConfigView(ConfigRevision):
    declared: dict[str, Any]
    effective: dict[str, Any]
    provenance: ConfigProvenance


class StreamCounts(BaseModel):
    cameras: int
    enabled: int
    running: int
    replays: int


class MediaServerStatus(BaseModel):
    port: int
    running: bool
    pid: int | None
    restarts: int


class EngineStatus(BaseModel):
    instance_id: str
    api_version: Literal[1]
    version: str
    pid: int
    started_at: float
    engine: EngineState
    config: ConfigStatus
    counts: StreamCounts
    servers: list[MediaServerStatus]
    updated_at: float | None
    capabilities: list[str]


class OperationResult(BaseModel):
    id: str
    instance_id: str
    kind: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    created_at: float
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: ErrorDetails | None = None
    config_persisted: bool
    applied: bool


class CameraView(CameraRef):
    enabled: bool
    state: str
    url: str
    source: str
    live: bool
    sync_group: str | None
    simulation: str
    spec: dict[str, Any]
    runtime: dict[str, Any] | None = None


class CameraList(BaseModel):
    revision: str | None
    cameras: list[CameraView]


class CameraDetail(BaseModel):
    revision: str | None
    camera: CameraView


class PlanResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    plan_id: str
    base_revision: str
    proposed: bool
    added: list[CameraRef]
    removed: list[CameraRef]
    updated: list[CameraRef]
    disruptive: bool
    disruptive_reasons: list[str]
    restart_required: list[str]


class StreamLink(CameraRef):
    protocol: Literal["rtsp"] = "rtsp"
    kind: Literal["camera", "replay"]
    url: str


class StreamLinks(BaseModel):
    urls: list[str]
    streams: list[StreamLink]
