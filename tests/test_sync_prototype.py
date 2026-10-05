"""The prototype's deterministic contract tests do not require a running rig."""

from __future__ import annotations

import copy
import multiprocessing
import os
import queue
import signal
import socket
import struct
import time
from fractions import Fraction
from pathlib import Path

import pytest
from scripts.sync_prototype.clock import SceneClock, alignment_report
from scripts.sync_prototype.events import MAX_EVENT_BYTES, EventReceiver, EventSink
from scripts.sync_prototype.proxy import Proxy
from scripts.sync_prototype.validation import validate_report


def test_clock_uses_exact_rational_deadlines_over_a_day() -> None:
    clock = SceneClock(123456789, Fraction(30000, 1001), frames=120, lead_frames=0)
    frame = 2_589_411
    assert clock.deadline_ns(frame) == 123456789 + frame * 1001 * 1_000_000_000 // 30000
    assert clock.global_frame(clock.deadline_ns(frame) + 1) == frame
    assert clock.deadline_ns(120) - clock.deadline_ns(0) == 4_004_000_000


def test_b_frame_decode_deadline_preserves_preroll() -> None:
    clock = SceneClock(1_000_000_000)
    assert clock.deadline_ns(-2) == 1_033_333_333
    assert clock.deadline_ns(0) == 1_100_000_000


def test_recovery_selects_a_future_access_point_across_scene_wrap() -> None:
    clock = SceneClock(0, lead_frames=0)
    assert clock.future_access_point(clock.deadline_ns(112), (0, 30, 60, 90), 15) == 150
    assert clock.future_access_point(clock.deadline_ns(45), (0, 30, 60, 90), 15) == 60
    with pytest.raises(ValueError, match="no independently decodable"):
        clock.future_access_point(0, (), 15)


def test_maximum_not_percentile_is_the_alignment_gate() -> None:
    samples = [
        {"global_frame": 0, "view": view, "arrival_ns": delay}
        for view, delay in enumerate((0, 1_000_000, 34_000_000))
    ]
    report = alignment_report(samples, 1000 / 30)
    assert report["matched_frames"] == 1
    assert report["violations"] == 1
    assert report["receive_spread_ms"]["max"] == 34


def test_missing_or_duplicate_frames_are_not_manufactured_as_matches() -> None:
    samples = [
        {"global_frame": 0, "view": 0, "arrival_ns": 0},
        {"global_frame": 0, "view": 0, "arrival_ns": 1},
        {"global_frame": 0, "view": 1, "arrival_ns": 2},
    ]
    report = alignment_report(samples, 1000 / 30)
    assert report["matched_frames"] == 0
    assert report["duplicates"] == 1
    assert report["receive_spread_ms"]["max"] is None


def test_peer_alignment_remains_measurable_during_member_outage() -> None:
    samples = [
        {"global_frame": 400, "view": 0, "arrival_ns": 0},
        {"global_frame": 400, "view": 2, "arrival_ns": 34_000_000},
    ]
    assert alignment_report(samples, 1000 / 30)["matched_frames"] == 0
    peers = alignment_report(samples, 1000 / 30, expected_views=(0, 2))
    assert peers["matched_frames"] == 1
    assert peers["violations"] == 1


def test_server_readiness_requires_the_owned_listener_and_exact_port() -> None:
    from scripts.sync_prototype.__main__ import listener_reported, server_config

    assert listener_reported("[RTSP] started with listeners on 127.0.0.1:1234 (TCP/RTSP)", 1234)
    assert listener_reported("[RTSP] listener opened on 127.0.0.1:1234 (TCP)", 1234)
    assert not listener_reported("[RTSP] listener opened on 127.0.0.1:12345 (TCP)", 1234)
    assert not listener_reported("[RTSP] listener opened on :1234 (TCP)", 1234)
    config = server_config(1234)
    assert config["rtspAddress"] == "127.0.0.1:1234"
    assert config["rtspTransports"] == ["tcp"]
    assert config["api"] is False


def test_telemetry_is_atomic_bounded_and_detects_sequence_gaps() -> None:
    receiver = EventReceiver()
    sink = receiver.sink()
    try:
        sink.put({"kind": "ready"})
        assert receiver.get(0.1) == {"kind": "ready"}
        sink.sequence += 1
        sink.put({"kind": "sent"})
        assert receiver.get(0.1) == {"kind": "sent"}
        assert receiver.gaps[0]["previous"] == 1
        with pytest.raises(queue.Empty):
            receiver.get(0.01)
        with pytest.raises(ValueError, match="bounded datagram"):
            sink.put({"detail": "x" * MAX_EVENT_BYTES})
    finally:
        receiver.close()
        if sink._socket is not None:
            sink._socket.close()


def test_telemetry_collector_drains_bursts_before_watchdog_polling() -> None:
    receiver = EventReceiver()
    sink = receiver.sink()
    try:
        for index in range(600):
            sink.put({"kind": "sent", "index": index})
        sink.finish()

        from scripts.sync_prototype.__main__ import drain_events

        events = list(drain_events(receiver, timeout=0.1))

        assert len(events) == 601
        assert [event["index"] for event in events[:-1]] == list(range(600))
        assert events[-1]["kind"] == "telemetry_end"
        assert not receiver.gaps
        assert not receiver.evidence()["incomplete"]
    finally:
        receiver.close()
        if sink._socket is not None:
            sink._socket.close()


def _suspended_sender(sink: EventSink, stop) -> None:
    sink.put({"kind": "ready"})
    os.kill(os.getpid(), signal.SIGSTOP)
    stop.wait(5)


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="requires POSIX process suspension")
def test_suspended_child_cannot_block_healthy_telemetry_or_cleanup() -> None:
    from scripts.sync_prototype.media import stop_process

    ctx = multiprocessing.get_context("spawn")
    receiver = EventReceiver()
    stop = ctx.Event()
    process = ctx.Process(target=_suspended_sender, args=(receiver.sink(), stop))
    healthy = receiver.sink()
    process.start()
    try:
        assert receiver.get(5) == {"kind": "ready"}
        time.sleep(0.05)
        healthy.put({"kind": "healthy"})
        assert receiver.get(0.2) == {"kind": "healthy"}
        began = time.monotonic()
        stop_process(process, stop, timeout=0.5)
        assert time.monotonic() - began < 2
        assert not process.is_alive()
        assert not receiver.gaps
    finally:
        stop_process(process, stop, timeout=0.5)
        receiver.close()
        if healthy._socket is not None:
            healthy._socket.close()


def test_private_telemetry_rejects_unauthenticated_and_oversized_messages() -> None:
    receiver = EventReceiver()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b'{"token":"wrong"}', receiver.socket.getsockname())
            with pytest.raises(ValueError, match="unexpected sender"):
                receiver.get(0.1)
            sender.sendto(b"x" * (MAX_EVENT_BYTES + 1), receiver.socket.getsockname())
            with pytest.raises(ValueError, match="oversized"):
                receiver.get(0.1)
    finally:
        receiver.close()


def test_proxy_records_rtp_and_sender_reports_instead_of_treating_them_as_scene_truth() -> None:
    from vcam.rtsp_messages import build_interleaved

    with socket.socket() as target:
        target.bind(("127.0.0.1", 0))
        target.listen()
        proxy = Proxy(target.getsockname()[1], inspect=True)
        try:
            with socket.create_connection(("127.0.0.1", proxy.port)) as client:
                server, _ = target.accept()
                with server:
                    data = bytes.fromhex("80e0000100000bb800000001") + b"payload"
                    server.sendall(build_interleaved(0, data))
                    client.settimeout(2)
                    assert client.recv(1024) == build_interleaved(0, data)
                    assert proxy.records[0]["timestamp"] == 3000
                    assert proxy.records[0]["ssrc"] == 1
                    sr = struct.pack(
                        "!BBHIIIIII", 0x80, 200, 6, 1, 2208988800 + 1234, 1 << 31, 9000, 0, 0
                    )
                    server.sendall(build_interleaved(1, sr))
                    received = bytearray()
                    while len(received) < len(sr) + 4:
                        received.extend(client.recv(1024))
                    assert proxy.records[1]["kind"] == "sr"
                    assert proxy.records[1]["ntp_unix"] == 1234.5
                    assert proxy.records[1]["timestamp"] == 9000
        finally:
            proxy.close()
        assert not proxy.errors


def test_validation_rejects_a_healthy_reader_that_silently_ends_early() -> None:
    samples = [
        {
            "global_frame": frame,
            "marker": frame % 120,
            "view": view,
            "name": f"main{view}",
            "arrival_ns": SceneClock(0).deadline_ns(frame),
            "observer_session": 0,
            "publisher_generation": 0,
            "pts_seconds": frame / 30,
            "eligible": True,
        }
        for frame in range(810)
        for view in range(3)
    ]
    report = {
        "scenario": "baseline",
        "duration_seconds": 28,
        "epoch_ns": 0,
        "samples": samples,
        "alignment": alignment_report(samples, 1000 / 30),
        "events": [],
        "errors": [],
    }
    validate_report(report)
    assert report["verdict"] == "failed"
    assert sum(error["kind"] == "reader_stopped_decoding" for error in report["errors"]) == 5


def acceptance_report() -> dict:
    samples = []
    for name, view, first, last in (
        ("main0", 0, 0, 1317),
        ("main1", 1, 0, 1317),
        ("main2", 2, 0, 1317),
        ("late", 0, 150, 267),
        ("reconnect", 0, 270, 1317),
    ):
        for frame in range(first, last):
            if name == "main1" and 360 <= frame < 420:
                continue
            restarted = name == "main1" and frame >= 420
            samples.append(
                {
                    "name": name,
                    "view": view,
                    "global_frame": frame,
                    "marker": frame % 120,
                    "arrival_ns": SceneClock(0).deadline_ns(frame),
                    "pts_seconds": (frame - 420 if restarted else frame) / 30,
                    "observer_session": int(restarted),
                    "publisher_generation": int(restarted),
                    "eligible": True,
                }
            )
    return {
        "scenario": "exit",
        "duration_seconds": 44,
        "epoch_ns": 0,
        "samples": samples,
        "errors": [],
        "events": [
            {"kind": "fault_begin", "at_ns": 12_000_000_000},
            {
                "kind": "observed_recovery",
                "at_ns": 14_100_000_000,
                "confirmed_at_ns": 15_066_666_666,
                "consecutive_frames": 30,
            },
            {"kind": "sent", "view": 1, "generation": 1, "global_frame": 900, "lateness_ms": 0},
        ],
    }


def test_acceptance_report_requires_sustained_current_decoded_readers() -> None:
    report = acceptance_report()
    validate_report(report)
    assert report["verdict"] == "passed_controlled_profile", report["errors"]


@pytest.mark.parametrize("lag", [24, 120])
def test_stale_late_and_reconnected_readers_fail_even_with_consistent_pts(lag: int) -> None:
    report = acceptance_report()
    for sample in report["samples"]:
        if sample["name"] in ("late", "reconnect"):
            sample["global_frame"] -= lag
            sample["marker"] = sample["global_frame"] % 120
    validate_report(report)
    assert report["verdict"] == "failed"
    assert any(error["kind"] == "reader_not_current" for error in report["errors"])


def test_recovered_reader_ending_early_is_not_exempt() -> None:
    report = acceptance_report()
    report["samples"] = [
        sample
        for sample in report["samples"]
        if sample["name"] != "main1" or sample["arrival_ns"] < 30_000_000_000
    ]
    validate_report(report)
    assert report["alignment"]["matched_frames"] > 44 * 30 / 2
    assert {"kind": "reader_stopped_decoding", "name": "main1"} in report["errors"]


def test_global_identity_rejects_a_whole_scene_lag_on_all_readers() -> None:
    report = acceptance_report()
    for sample in report["samples"]:
        sample["global_frame"] -= 120
    validate_report(report)
    assert report["verdict"] == "failed"
    assert any(error["kind"] == "reader_not_current" for error in report["errors"])


def test_recovery_window_resets_for_each_publication_and_requires_sustained_progress() -> None:
    from scripts.sync_prototype.recovery import RecoveryWindow

    window = RecoveryWindow()
    window.begin(1)
    assert window.expects_reader_failure("main1", 1, {("main1", 0)})
    assert not window.expects_reader_failure("main0", 0, {("main1", 0)})
    samples = [
        sample
        for sample in acceptance_report()["samples"]
        if sample["name"] == "main1" and sample["publisher_generation"] == 1
    ]
    for sample in samples[:29]:
        assert window.observe(sample) is None
    assert window.observe(samples[29])["consecutive_frames"] == 30
    assert not window.active
    assert window.expects_reader_failure("main1", 0, {("main1", 0)})
    assert not window.expects_reader_failure("main1", 1, {("main1", 0)})
    window.begin(2)
    assert window.active
    assert window.frames == 0
    assert window.observe(samples[30]) is None
    candidate = copy.deepcopy(samples[30])
    candidate["publisher_generation"] = 2
    assert window.observe(candidate) is None
    assert window.frames == 1


def test_a_second_post_recovery_error_cannot_be_hidden_by_later_success() -> None:
    report = acceptance_report()
    report["errors"].append({"kind": "post_recovery_publisher_failure", "reason": "exit"})
    validate_report(report)
    assert report["verdict"] == "failed"


@pytest.mark.parametrize("kind", ["restart", "skipped"])
def test_publisher_interruption_after_confirmed_recovery_fails(kind: str) -> None:
    report = acceptance_report()
    report["events"].append({"kind": kind, "view": 1, "at_ns": 30_000_000_000})
    validate_report(report)
    assert {"kind": "post_recovery_publisher_interrupted"} in report["errors"]


def test_terminal_record_is_not_a_license_for_later_unaccounted_data() -> None:
    receiver = EventReceiver()
    sink = receiver.sink()
    try:
        sink.finish()
        receiver.get(0.1)
        sink.put({"kind": "sent"})
        with pytest.raises(ValueError, match=r"after.*terminal"):
            receiver.get(0.1)
    finally:
        receiver.close()
        if sink._socket is not None:
            sink._socket.close()


def test_missing_terminal_telemetry_is_incomplete_even_without_sequence_gaps() -> None:
    receiver = EventReceiver()
    sink = receiver.sink()
    try:
        sink.put({"kind": "ready"})
        receiver.get(0.1)
        assert not receiver.gaps
        assert receiver.evidence()["incomplete"] == [sink.producer]
        sink.finish()
        assert receiver.get(0.1)["kind"] == "telemetry_end"
        assert not receiver.evidence()["incomplete"]
    finally:
        receiver.close()
        if sink._socket is not None:
            sink._socket.close()


def test_cleanup_failure_does_not_skip_later_owned_resources() -> None:
    from scripts.sync_prototype.lifecycle import cleanup_all

    visited = []

    def fail() -> None:
        raise RuntimeError("injected cleanup failure")

    errors = []
    cleanup_all([("failed", fail), ("next", lambda: visited.append("next"))], errors)
    assert visited == ["next"]
    assert errors == [
        {"kind": "cleanup_error", "resource": "failed", "detail": "injected cleanup failure"}
    ]


def _hung_experiment(connection, duration, b_frames, scenario, binary, directory) -> None:
    os.setsid()
    connection.send(os.getpid())
    connection.close()
    time.sleep(60)


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="requires POSIX process groups")
def test_outer_deadline_actually_stops_a_hung_experiment(tmp_path: Path) -> None:
    from scripts.sync_prototype.lifecycle import run_bounded

    started = time.monotonic()
    report = run_bounded(
        28, 0, "baseline", Path("unused"), tmp_path, timeout=2, target=_hung_experiment
    )
    assert time.monotonic() - started < 7
    assert report["errors"] == [{"kind": "experiment_deadline"}]


@pytest.mark.parametrize("b_frames", [0, 2])
def test_generated_fixtures_have_verified_markers_and_complete_packet_timing(
    tmp_path: Path, b_frames: int
) -> None:
    av = pytest.importorskip("av")
    pytest.importorskip("numpy")
    from scripts.sync_prototype.media import FRAMES, generate_fixture, packet_index

    path = tmp_path / "synthetic.mp4"
    manifest = generate_fixture(path, 2, b_frames)
    assert manifest["frames"] == FRAMES
    with av.open(str(path)) as source:
        packets, points = packet_index(source)
    assert points == (0, 30, 60, 90)
    assert len(packets) == FRAMES
    assert packets[0]["dts"] == -b_frames


@pytest.mark.parametrize("b_frames", [0, 2])
def test_copy_packets_carry_unique_decoded_identity_without_reencoding(
    tmp_path: Path, b_frames: int
) -> None:
    av = pytest.importorskip("av")
    pytest.importorskip("numpy")
    from scripts.sync_prototype.identity import decoded_identity, identified_packet
    from scripts.sync_prototype.media import generate_fixture, read_marker

    path = tmp_path / "identity.mp4"
    generate_fixture(path, 1, b_frames)
    with av.open(str(path)) as source:
        stream = source.streams.video[0]
        frames = []
        for packet in source.demux(stream):
            if not packet.size:
                continue
            global_frame = round(packet.pts * packet.time_base * 30) + 1200
            payload = identified_packet(bytes(packet), 1, 3, global_frame)
            assert payload.endswith(bytes(packet))
            tagged = av.Packet(payload)
            tagged.pts, tagged.dts = packet.pts, packet.dts
            tagged.time_base = packet.time_base
            frames.extend(stream.codec_context.decode(tagged))
        frames.extend(stream.codec_context.decode(None))
    assert len(frames) == 120
    for index, frame in enumerate(frames):
        assert read_marker(frame) == (1, index)
        assert decoded_identity(frame) == (1, 3, 1200 + index)


def test_a_timestamped_frame_without_diagnostic_identity_is_not_accepted() -> None:
    av = pytest.importorskip("av")
    from scripts.sync_prototype.identity import decoded_identity

    frame = av.VideoFrame(192, 96, format="yuv420p")
    frame.pts = 0
    with pytest.raises(ValueError, match="complete diagnostic identity"):
        decoded_identity(frame)


def test_markers_fail_explicitly_when_unreadable() -> None:
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    from scripts.sync_prototype.media import read_marker

    frame = av.VideoFrame.from_ndarray(np.full((96, 192, 3), 128, dtype=np.uint8), format="rgb24")
    with pytest.raises(ValueError, match="undecodable"):
        read_marker(frame)


@pytest.mark.skipif(
    os.environ.get("VCAM_SYNC_INTEGRATION") != "1",
    reason="opt-in local RTSP experiment; set VCAM_SYNC_INTEGRATION=1 and install prototype extra",
)
@pytest.mark.parametrize(
    ("scenario", "b_frames"),
    [("baseline", 0), ("baseline", 2), ("exit", 2), ("stall", 0), ("backpressure", 0)],
)
def test_local_rtsp_prototype(tmp_path: Path, scenario: str, b_frames: int) -> None:
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    from scripts.sync_prototype.lifecycle import run_bounded

    from vcam.binaries import resolve_binary

    start = time.monotonic()
    report = run_bounded(44, b_frames, scenario, resolve_binary(allow_download=False), tmp_path)
    assert time.monotonic() - start < 90
    assert report["verdict"] == "passed_controlled_profile", report["errors"]
    assert report["alignment"]["receive_spread_ms"]["max"] <= 1000 / 30
    assert not report["telemetry"]["incomplete"]
    assert not report["telemetry"]["gaps"]
    assert all(
        value["max_receive_spread_ms"] <= 1000 / 30 for value in report["join_alignment"].values()
    )
