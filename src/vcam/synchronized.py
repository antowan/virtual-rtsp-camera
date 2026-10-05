"""Admitted H.264 copy publishers with one scene epoch and isolated recovery."""

from __future__ import annotations

import hashlib
import logging
import multiprocessing as mp
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from .errors import SupervisorError
from .scene_clock import SceneClock

logger = logging.getLogger("vcam")
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_GROUP_BYTES = 256 * 1024 * 1024
MAX_SOURCE_FRAMES = 108_000


def media_backend() -> Any:
    try:
        import av
    except ImportError as exc:
        raise SupervisorError(
            "Synchronized playback requires PyAV: install `vcam[sync]` "
            "or run `uv sync --extra sync`."
        ) from exc
    return av


@dataclass(frozen=True)
class CopyPacket:
    pts: int
    dts: int
    data: bytes
    idr: bool


@dataclass(frozen=True)
class CopySource:
    path: Path
    rate: Fraction
    packets: tuple[CopyPacket, ...]
    points: tuple[int, ...]
    lead_frames: int
    sha256: str

    @property
    def frames(self) -> int:
        return len(self.packets)

    @property
    def byte_size(self) -> int:
        return sum(len(packet.data) for packet in self.packets)


def has_idr(data: bytes) -> bool:
    position = 0
    types = []
    while position < len(data):
        if position + 4 > len(data):
            raise ValueError("truncated AVCC NAL length")
        size = int.from_bytes(data[position : position + 4], "big")
        position += 4
        if size == 0 or position + size > len(data):
            raise ValueError("invalid AVCC NAL size")
        types.append(data[position] & 31)
        position += size
    return 5 in types


def admit_source(path: Path) -> CopySource:
    """Validate complete native timing and independently decodable closed GOPs."""
    av = media_backend()
    try:
        with av.open(str(path)) as source:
            if len(source.streams.video) != 1:
                raise ValueError("exactly one video stream is required")
            stream = source.streams.video[0]
            rate = Fraction(stream.average_rate or 0)
            if stream.codec_context.name != "h264" or not 1 <= rate <= 120:
                raise ValueError(
                    "H.264 with a constant native frame rate from 1 to 120 is required"
                )
            extra = stream.codec_context.extradata
            if not extra or len(extra) < 5 or extra[0] != 1 or extra[4] & 3 != 3:
                raise ValueError("MP4-style four-byte AVCC NAL lengths are required")
            packets: list[CopyPacket] = []
            byte_size = 0
            for packet in source.demux(stream):
                if not packet.size:
                    continue
                if packet.pts is None or packet.dts is None or packet.time_base is None:
                    raise ValueError("every access unit must have PTS, DTS and time base")
                pts = Fraction(packet.pts) * packet.time_base * rate
                dts = Fraction(packet.dts) * packet.time_base * rate
                duration = Fraction(packet.duration) * packet.time_base * rate
                if pts.denominator != 1 or dts.denominator != 1 or duration != 1:
                    raise ValueError("access units must have integral, constant one-frame timing")
                data = bytes(packet)
                byte_size += len(data)
                if byte_size > MAX_SOURCE_BYTES or len(packets) >= MAX_SOURCE_FRAMES:
                    raise ValueError("source exceeds the 64 MiB / 108000-frame admission limit")
                packets.append(CopyPacket(int(pts), int(dts), data, has_idr(data)))
            count = len(packets)
            if not count or sorted(packet.pts for packet in packets) != list(range(count)):
                raise ValueError("presentation frames must cover one scene starting at PTS zero")
            if any(after.dts != before.dts + 1 for before, after in pairwise(packets)):
                raise ValueError("DTS must advance exactly one native frame per access unit")
            points = tuple(packet.pts for packet in packets if packet.idr)
            if not points or points[0] != 0 or tuple(sorted(points)) != points:
                raise ValueError("scene must start with an IDR and have ordered IDR access points")
            lead = max(1, 1 - packets[0].dts, *(packet.pts - packet.dts + 1 for packet in packets))
            if packets[0].dts > 0 or lead > 32:
                raise ValueError("unsupported decode preroll / reordering depth")
            offsets = [index for index, packet in enumerate(packets) if packet.idr]
            for start, end in pairwise([*offsets, count]):
                first = packets[start].pts
                last = packets[end].pts if end < count else count
                if Fraction(last - first, rate) > 10:
                    raise ValueError("IDR spacing must not exceed ten seconds")
                decoder = av.CodecContext.create("h264", "r")
                decoder.extradata = extra
                decoded_pts: list[int | None] = []
                for original in packets[start:end]:
                    tagged = av.Packet(original.data)
                    tagged.pts, tagged.dts = original.pts, original.dts
                    tagged.time_base = 1 / rate
                    decoded_pts.extend(frame.pts for frame in decoder.decode(tagged))
                decoded_pts.extend(frame.pts for frame in decoder.decode(None))
                if decoded_pts != list(range(first, last)):
                    raise ValueError("each IDR segment must decode its complete closed GOP")
        return CopySource(
            path.resolve(),
            rate,
            tuple(packets),
            points,
            lead,
            _file_hash(path),
        )
    except (OSError, ValueError, av.error.FFmpegError) as exc:
        raise SupervisorError(f"Cannot synchronize {path}: {exc}") from exc


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compatible_sources(sources: list[CopySource]) -> None:
    if not sources:
        raise SupervisorError("synchronization group has no sources")
    if len({(source.rate, source.frames) for source in sources}) != 1:
        raise SupervisorError("synchronization group sources must have identical rate/frame count")
    if sum(source.byte_size for source in sources) > MAX_GROUP_BYTES:
        raise SupervisorError("synchronization group exceeds its 256 MiB compressed-source limit")


def _publisher(
    source: CopySource,
    url: str,
    generation: int,
    lead: int,
    events: Connection,
    commands: Connection,
) -> None:
    # Spawned children must not execute the supervisor's inherited signal handler.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    av = media_backend()
    try:
        if _file_hash(source.path) != source.sha256:
            raise ValueError("source changed after admission; restart vcam to admit it again")
        with (
            av.open(str(source.path)) as template,
            av.open(
                url, "w", format="rtsp", options={"rtsp_transport": "tcp"}, timeout=(3.0, 0.5)
            ) as output,
        ):
            stream = output.add_stream_from_template(template.streams.video[0])
            output.start_encoding()
            events.send({"kind": "ready"})
            command = commands.recv()
            if command["kind"] == "stop":
                return
            clock = SceneClock(command["epoch_ns"], source.rate, source.frames, lead)
            target = (
                0
                if generation == 0
                else clock.future_access_point(
                    time.monotonic_ns(), source.points, lead + max(1, int(source.rate / 2))
                )
            )
            origin = target
            position = next(
                index
                for index, packet in enumerate(source.packets)
                if packet.pts == target % source.frames
            )
            loop = target // source.frames
            reported = 0
            while True:
                original = source.packets[position]
                pts = loop * source.frames + original.pts
                dts = loop * source.frames + original.dts
                deadline = clock.deadline_ns(dts)
                events.send({"kind": "scheduled", "deadline_ns": deadline, "target": pts})
                if commands.poll(max(0, (deadline - time.monotonic_ns()) / 1_000_000_000)):
                    commands.recv()
                    return
                now = time.monotonic_ns()
                if now - deadline > int(1_000_000_000 / source.rate):
                    target = clock.future_access_point(
                        now, source.points, lead + max(1, int(source.rate / 2))
                    )
                    position = next(
                        index
                        for index, packet in enumerate(source.packets)
                        if packet.pts == target % source.frames
                    )
                    loop = target // source.frames
                    events.send({"kind": "skipped", "from": pts, "target": target})
                    continue
                packet = av.Packet(original.data)
                packet.pts, packet.dts = pts - origin, dts - origin
                packet.duration = 1
                packet.time_base = 1 / source.rate
                packet.is_keyframe = original.idr
                packet.stream = stream
                output.mux(packet)
                sent = time.monotonic_ns()
                if sent - reported >= 100_000_000:
                    events.send(
                        {
                            "kind": "sent",
                            "sent_ns": sent,
                            "global_frame": pts,
                            "generation": generation,
                        }
                    )
                    reported = sent
                position += 1
                if position == source.frames:
                    position = 0
                    loop += 1
    except Exception as exc:
        events.send({"kind": "error", "detail": str(exc)})
        raise
    finally:
        events.close()
        commands.close()


class SyncMember:
    def __init__(self, name: str, source: CopySource, url: str, group: SyncGroup) -> None:
        self.name, self.source, self.url, self.group = name, source, url, group
        self.process: Any = None
        self.receiver: Connection | None = None
        self.commands: Connection | None = None
        self.restarts = 0
        self.last_exit_code: int | None = None
        self.last_progress_ns = 0
        self.deadline_ns = 0
        self.started_ns = 0
        self.retry_at = 0.0
        self.failures = 0
        self.gave_up = False
        self.ready = False
        self.state = "stopped"
        self.last_error: str | None = None
        self.global_frame: int | None = None
        self.skips = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.is_alive()

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def start(self) -> None:
        self.receiver, sender = self.group.context.Pipe(duplex=False)
        commands, self.commands = self.group.context.Pipe(duplex=False)
        self.ready = False
        self.state = "starting"
        self.started_ns = time.monotonic_ns()
        self.last_progress_ns = self.deadline_ns = 0
        self.process = self.group.context.Process(
            target=_publisher,
            args=(
                self.source,
                self.url,
                self.restarts,
                self.group.lead,
                sender,
                commands,
            ),
            name=f"vcam-sync-{self.name}",
        )
        try:
            self.process.start()
            if self.group.epoch_ns:
                self.release()
        except Exception:
            self.receiver.close()
            self.commands.close()
            self.process = None
            raise
        finally:
            sender.close()
            commands.close()

    def release(self) -> None:
        if self.commands is None:
            raise SupervisorError(f"{self.name}: publisher has no command channel")
        self.commands.send({"kind": "start", "epoch_ns": self.group.epoch_ns})

    def drain(self) -> None:
        if self.receiver is None:
            return
        while self.receiver.poll():
            try:
                event = self.receiver.recv()
            except EOFError:
                self.receiver.close()
                self.receiver = None
                return
            kind = event["kind"]
            if kind == "ready":
                self.ready = True
                self.state = "waiting"
            elif kind == "scheduled":
                self.deadline_ns = event["deadline_ns"]
            elif kind == "sent":
                self.last_progress_ns = event["sent_ns"]
                self.global_frame = event["global_frame"]
                self.state = "publishing"
            elif kind == "skipped":
                self.skips += 1
                self.state = "recovering"
                logger.warning("%s: skipping stale data to IDR %s", self.name, event["target"])
            elif kind == "error":
                self.last_error = event["detail"]
                logger.error("%s: synchronized publisher failed: %s", self.name, self.last_error)

    def stop(self, timeout: float = 1.0) -> None:
        if self.process is not None:
            if self.commands is not None and self.running:
                try:
                    self.commands.send({"kind": "stop"})
                except BrokenPipeError:
                    logger.debug("%s: stopped before receiving shutdown command", self.name)
            self.process.join(timeout)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout)
            if self.process.is_alive():
                raise SupervisorError(f"synchronized publisher {self.name} did not stop")
            self.last_exit_code = self.process.exitcode
            self.process.close()
            self.process = None
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None
        if self.commands is not None:
            self.commands.close()
            self.commands = None
        self.state = "stopped"

    def tick(self, now: float) -> None:
        self.drain()
        now_ns = time.monotonic_ns()
        if self.running:
            stale = (
                now_ns - self.started_ns > 5_000_000_000
                and now_ns - max(self.deadline_ns, self.last_progress_ns, self.started_ns)
                > 2_000_000_000
            )
            if not stale:
                return
            self.last_error = "publisher write-progress watchdog expired"
            logger.error("%s: %s", self.name, self.last_error)
        if self.process is not None:
            if self.last_progress_ns - self.started_ns >= 20_000_000_000:
                self.failures = 0
            self.stop()
            self.failures += 1
            self.retry_at = now + min(30.0, 2 ** min(self.failures - 1, 5))
            self.state = "recovering"
        if now < self.retry_at or self.gave_up:
            return
        if self.group.max_restarts is not None and self.restarts >= self.group.max_restarts:
            self.gave_up = True
            self.state = "failed"
            logger.error("%s: exhausted synchronized publisher restart budget", self.name)
            return
        self.restarts += 1
        logger.warning("%s: synchronized restart #%s", self.name, self.restarts)
        self.start()

    def health(self) -> dict[str, Any]:
        return {
            "sync_group": self.group.name,
            "state": self.state,
            "global_frame": self.global_frame,
            "generation": self.restarts,
            "last_progress_ns": self.last_progress_ns or None,
            "last_error": self.last_error,
            "gop_skips": self.skips,
            "gave_up": self.gave_up,
        }


class SyncGroup:
    def __init__(
        self, name: str, sources: list[tuple[str, CopySource, str]], max_restarts: int | None
    ) -> None:
        compatible_sources([source for _, source, _ in sources])
        self.name, self.max_restarts = name, max_restarts
        self.context = mp.get_context("spawn")
        self.epoch_ns = 0
        self.lead = max(source.lead_frames for _, source, _ in sources)
        self.members = [SyncMember(name, source, url, self) for name, source, url in sources]

    def start(self, service: Callable[[], None] | None = None) -> None:
        try:
            for member in self.members:
                member.start()
            deadline = time.monotonic() + 15
            while True:
                if service is not None:
                    service()
                for member in self.members:
                    member.drain()
                    if not member.running or member.last_error is not None:
                        raise SupervisorError(
                            f"sync_group {self.name}: {member.name} failed startup: "
                            f"{member.last_error or 'publisher exited'}"
                        )
                if all(member.ready for member in self.members):
                    break
                if time.monotonic() > deadline:
                    raise SupervisorError(f"sync_group {self.name}: publisher barrier timed out")
                time.sleep(0.01)
            self.epoch_ns = time.monotonic_ns() + 1_000_000_000
            for member in self.members:
                member.release()
            logger.info("sync_group %s: started %s publishers", self.name, len(self.members))
        except BaseException:
            self.stop()
            raise

    def tick(self, now: float) -> None:
        for member in self.members:
            member.tick(now)

    def stop(self) -> None:
        errors = []
        for member in self.members:
            try:
                member.stop()
            except SupervisorError as exc:
                errors.append(str(exc))
        if errors:
            raise SupervisorError("; ".join(errors))
