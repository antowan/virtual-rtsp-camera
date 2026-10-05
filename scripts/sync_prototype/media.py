"""Synthetic pixel markers and copy workers with persistent RTSP output contexts."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import time
import traceback
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

from .clock import SceneClock
from .identity import decoded_identity, identified_packet

RATE = 30
FRAMES = 120
GOP = 30
WIDTH = 192
HEIGHT = 96
BLOCK = 12
BITS = 12
MAX_FIXTURE_BYTES = 16 * 1024 * 1024


def dependencies():
    try:
        import av
        import numpy
    except ImportError as exc:
        raise RuntimeError(
            "Install prototype dependencies with `uv sync --extra prototype`."
        ) from exc
    return av, numpy


def marker_image(view: int, frame: int):
    _, np = dependencies()
    image = np.random.default_rng(view * FRAMES + frame).integers(
        0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8
    )
    value = (view << 8) | frame
    for bit in range(BITS):
        level = 224 if value & (1 << bit) else 24
        image[12:36, bit * BLOCK : (bit + 1) * BLOCK, :] = level
        image[48:72, bit * BLOCK : (bit + 1) * BLOCK, :] = 248 - level
    image[80:88, frame % WIDTH : frame % WIDTH + 4, :] = 240
    return image


def read_marker(frame) -> tuple[int, int]:
    image = frame.to_ndarray(format="rgb24")
    value = 0
    for bit in range(BITS):
        normal = float(image[16:32, bit * BLOCK + 3 : (bit + 1) * BLOCK - 3].mean())
        inverse = float(image[52:68, bit * BLOCK + 3 : (bit + 1) * BLOCK - 3].mean())
        if abs(normal - inverse) < 100:
            raise ValueError("undecodable scene marker")
        value |= int(normal > inverse) << bit
    view, scene = value >> 8, value & 255
    if view > 2 or scene >= FRAMES:
        raise ValueError(f"out-of-range marker {view}:{scene}")
    return view, scene


def generate_fixture(path: Path, view: int, b_frames: int) -> dict:
    av, _ = dependencies()
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=RATE)
        stream.width, stream.height = WIDTH, HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.options = {
            "preset": "veryfast",
            "crf": "18",
            "x264-params": (
                f"keyint={GOP}:min-keyint={GOP}:scenecut=0:open-gop=0:bframes={b_frames}:b-adapt=0"
            ),
        }
        for index in range(FRAMES):
            frame = av.VideoFrame.from_ndarray(marker_image(view, index), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, RATE)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    with av.open(str(path)) as source:
        decoded = list(source.decode(video=0))
        identities = [read_marker(frame) for frame in decoded]
        if identities != [(view, index) for index in range(FRAMES)]:
            raise ValueError("fixture marker sequence does not match source scene")
        timing = [Fraction(frame.pts) * frame.time_base for frame in decoded]
        if timing != [Fraction(index, RATE) for index in range(FRAMES)]:
            raise ValueError("fixture presentation timeline is incomplete")
    with av.open(str(path)) as source:
        packets, points = packet_index(source)
    reordered = any(after["pts"] < before["pts"] for before, after in pairwise(packets))
    if reordered != bool(b_frames):
        raise ValueError("fixture does not exercise the requested B-frame reorder profile")
    for point in points:
        with av.open(str(path)) as source:
            stream = source.streams.video[0]
            source.seek(int(Fraction(point, RATE) / stream.time_base), stream=stream)
            if read_marker(next(source.decode(video=0))) != (view, point):
                raise ValueError("fixture access point cannot decode independently")
    return {
        "view": view,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "frames": FRAMES,
        "rate": f"{RATE}/1",
        "b_frames": b_frames,
    }


def packet_index(source) -> tuple[list[dict], tuple[int, ...]]:
    stream = source.streams.video[0]
    if stream.codec_context.name != "h264" or stream.average_rate != Fraction(RATE):
        raise ValueError("prototype requires its generated native-rate H.264 fixtures")
    extra = stream.codec_context.extradata
    if not extra or len(extra) < 5 or extra[0] != 1 or extra[4] & 3 != 3:
        raise ValueError("prototype requires four-byte AVCC NAL lengths")
    packets = []
    total_bytes = 0
    for packet in source.demux(stream):
        if not packet.size:
            continue
        if packet.pts is None or packet.dts is None:
            raise ValueError("missing source PTS/DTS")
        pts = packet.pts * packet.time_base * RATE
        dts = packet.dts * packet.time_base * RATE
        if pts.denominator != 1 or dts.denominator != 1:
            raise ValueError("source packet is not on the native frame grid")
        payload = bytes(packet)
        total_bytes += len(payload)
        if total_bytes > MAX_FIXTURE_BYTES:
            raise ValueError("compressed fixture exceeds prototype memory limit")
        packets.append(
            {"pts": int(pts), "dts": int(dts), "key": packet.is_keyframe, "data": payload}
        )
    if sorted(packet["pts"] for packet in packets) != list(range(FRAMES)):
        raise ValueError("source packets do not cover exactly one complete scene")
    if any(after["dts"] <= before["dts"] for before, after in pairwise(packets)):
        raise ValueError("non-monotonic source DTS")
    points = tuple(packet["pts"] for packet in packets if packet["key"])
    if points != tuple(range(0, FRAMES, GOP)):
        raise ValueError("unexpected fixture access-point schedule")
    # This verifier applies only to locally generated, closed-GOP x264 fixtures.
    # Arbitrary H.264 K flags are not proof of independently decodable IDRs.
    return packets, points


def worker(
    path: str,
    url: str,
    view: int,
    generation: int,
    epoch,
    start,
    stop,
    events,
) -> None:
    import resource

    av, _ = dependencies()
    try:
        with av.open(path) as source:
            packets, points = packet_index(source)
            with av.open(
                url,
                "w",
                format="rtsp",
                options={"rtsp_transport": "tcp"},
                timeout=(3.0, 0.5),
            ) as output:
                stream = output.add_stream_from_template(source.streams.video[0])
                output.start_encoding()
                events.put({"kind": "ready", "view": view, "generation": generation})
                while not start.wait(0.05):
                    if stop.is_set():
                        return
                clock = SceneClock(epoch.value)
                target = (
                    0
                    if generation == 0
                    else clock.future_access_point(time.monotonic_ns(), points, 15)
                )
                session_origin = target
                position = next(
                    i for i, packet in enumerate(packets) if packet["pts"] == target % FRAMES
                )
                loop = target // FRAMES
                last_progress = 0
                events.put(
                    {"kind": "joined", "view": view, "generation": generation, "target": target}
                )
                while not stop.is_set():
                    original = packets[position]
                    pts = loop * FRAMES + original["pts"]
                    dts = loop * FRAMES + original["dts"]
                    deadline = clock.deadline_ns(dts)
                    now = time.monotonic_ns()
                    if now - deadline > 1_000_000_000 // RATE:
                        target = clock.future_access_point(now, points, 15)
                        position = next(
                            i
                            for i, packet in enumerate(packets)
                            if packet["pts"] == target % FRAMES
                        )
                        loop = target // FRAMES
                        events.put(
                            {
                                "kind": "skipped",
                                "view": view,
                                "generation": generation,
                                "at_ns": now,
                                "from": pts,
                                "target": target,
                            }
                        )
                        continue
                    if stop.wait(max(0, (deadline - now) / 1_000_000_000)):
                        return
                    packet = av.Packet(identified_packet(original["data"], view, generation, pts))
                    packet.pts = pts - session_origin
                    packet.dts = dts - session_origin
                    packet.duration = 1
                    packet.is_keyframe = original["key"]
                    packet.time_base = Fraction(1, RATE)
                    packet.stream = stream
                    output.mux(packet)
                    sent = time.monotonic_ns()
                    events.put(
                        {
                            "kind": "sent",
                            "view": view,
                            "generation": generation,
                            "global_frame": pts,
                            "dts_frame": dts,
                            "sent_ns": sent,
                            "lateness_ms": (sent - deadline) / 1_000_000,
                        }
                    )
                    if sent - last_progress > 1_000_000_000:
                        usage = resource.getrusage(resource.RUSAGE_SELF)
                        events.put(
                            {
                                "kind": "resource",
                                "view": view,
                                "generation": generation,
                                "cpu_seconds": usage.ru_utime + usage.ru_stime,
                                "maxrss_native": usage.ru_maxrss,
                            }
                        )
                        last_progress = sent
                    position += 1
                    if position == len(packets):
                        position = 0
                        loop += 1
    except Exception:
        # A failed experiment must retain its actual backend error, not look healthy.
        events.put(
            {
                "kind": "error",
                "view": view,
                "generation": generation,
                "detail": traceback.format_exc(),
            }
        )
        raise
    finally:
        events.finish()


def observer(url: str, view: int, name: str, session: int, start, stop, events) -> None:
    av, _ = dependencies()
    if not start.wait(20):
        events.put(
            {
                "kind": "observer_error",
                "name": name,
                "observer_session": session,
                "detail": "epoch timeout",
            }
        )
        events.finish()
        return
    try:
        with av.open(
            url,
            options={"rtsp_transport": "tcp", "probesize": "32", "analyzeduration": "0"},
            timeout=(3.0, 2.0),
        ) as source:
            previous_pts = None
            preroll = 0
            for frame in source.decode(video=0):
                if stop.is_set():
                    return
                arrival = time.monotonic_ns()
                actual_view, marker = read_marker(frame)
                if actual_view != view:
                    raise ValueError("view marker differs from subscribed path")
                if frame.pts is None:
                    if previous_pts is not None or preroll >= 2:
                        raise ValueError(
                            "decoded frame has no presentation timestamp after preroll"
                        )
                    preroll += 1
                    events.put(
                        {
                            "kind": "observer_preroll",
                            "name": name,
                            "marker": marker,
                            "reason": "decoder startup frame without PTS",
                            "arrival_ns": arrival,
                        }
                    )
                    continue
                identity_view, generation, global_frame = decoded_identity(frame)
                if identity_view != view or global_frame % FRAMES != marker:
                    raise ValueError("diagnostic identity differs from decoded scene marker")
                pts = float(frame.pts * frame.time_base)
                if previous_pts is not None and pts <= previous_pts:
                    raise ValueError("decoded presentation timestamps moved backward")
                previous_pts = pts
                events.put(
                    {
                        "kind": "observed",
                        "view": view,
                        "name": name,
                        "marker": marker,
                        "pts_seconds": pts,
                        "arrival_ns": arrival,
                        "observer_session": session,
                        "publisher_generation": generation,
                        "global_frame": global_frame,
                    }
                )
    except Exception:
        if not stop.is_set():
            events.put(
                {
                    "kind": "observer_error",
                    "name": name,
                    "observer_session": session,
                    "detail": traceback.format_exc(),
                }
            )
            raise
    finally:
        events.finish()


def stop_process(process: multiprocessing.Process, event, timeout: float = 2.0) -> None:
    if process.is_alive() and hasattr(signal, "SIGCONT"):
        pid = process.pid
        if pid is None:
            raise RuntimeError("live owned process has no PID")
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            process.join(timeout)
            return
    event.set()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(timeout)
    if process.is_alive():
        process.kill()
        process.join(timeout)
    if process.is_alive():
        raise RuntimeError(f"owned process {process.pid} did not stop")
