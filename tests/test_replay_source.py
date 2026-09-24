"""Turning a capture into a replayable session (TEST-005…TEST-008)."""

from __future__ import annotations

from pathlib import Path

import pytest

from captures import (
    CLIENT_RTP,
    SDP_TEXT,
    SERVER_RTP,
    rtp_packet,
    rtp_series,
    write_interleaved_capture,
    write_udp_capture,
)
from vcam import replay_source, sdp
from vcam.pcap import Datagram, PcapWriter
from vcam.replay_source import (
    MAX_CAPTURE_BYTES_ENV,
    ReplaySourceError,
    StreamClock,
    TcpStream,
    _heuristic_tracks,
)

# -- TCP reassembly ---------------------------------------------------------


def test_reassembly_orders_by_sequence_not_arrival() -> None:
    stream = TcpStream()
    stream.add(11, b"world", 2.0)
    stream.add(1, b"hello", 1.0)

    data, marks = stream.assemble()
    assert data == b"hello" + b"\x00" * 5 + b"world"
    assert marks == [(0, 1.0), (10, 2.0)]


def test_reassembly_collapses_a_retransmit() -> None:
    stream = TcpStream()
    stream.add(1, b"abcde", 1.0)
    stream.add(1, b"abcde", 1.5)
    stream.add(6, b"fghij", 2.0)
    data, _ = stream.assemble()
    assert data == b"abcdefghij"


def test_reassembly_keeps_the_longest_segment_at_an_offset() -> None:
    """A retransmit may coalesce two segments into a longer one."""
    stream = TcpStream()
    stream.add(1, b"abc", 1.0)
    stream.add(1, b"abcdef", 1.5)
    data, _ = stream.assemble()
    assert data == b"abcdef"


def test_reassembly_trims_overlap() -> None:
    stream = TcpStream()
    stream.add(1, b"abcdef", 1.0)
    stream.add(4, b"defghi", 2.0)
    data, _ = stream.assemble()
    assert data == b"abcdefghi"


def test_reassembly_zero_fills_a_gap_to_keep_offsets_true() -> None:
    """Later offsets must stay correct, or every frame after a loss is misdated."""
    stream = TcpStream()
    stream.add(1, b"abc", 1.0)
    stream.add(10, b"xyz", 2.0)
    data, marks = stream.assemble()
    assert data == b"abc" + b"\x00" * 6 + b"xyz"
    assert marks[-1] == (9, 2.0)


def test_stream_clock_dates_an_offset() -> None:
    clock = StreamClock([(0, 100.0), (50, 101.0), (120, 102.0)])
    assert clock.at(0) == 100.0
    assert clock.at(49) == 100.0
    assert clock.at(50) == 101.0
    assert clock.at(200) == 102.0


def test_stream_clock_without_marks_is_zero() -> None:
    assert StreamClock([]).at(10) == 0.0


# -- loading ----------------------------------------------------------------


def test_udp_capture_round_trips_every_packet(tmp_path: Path) -> None:
    path = tmp_path / "udp.pcap"
    packets = write_udp_capture(path)
    source = replay_source.load(path)

    assert source.handshake_found
    assert [entry.data for entry in source.timeline] == packets
    assert len(source.tracks) == 1
    assert source.tracks[0].payload_type == 96
    assert source.tracks[0].clock_rate == 90000


def test_udp_capture_excludes_packets_outside_the_negotiated_endpoints(tmp_path: Path) -> None:
    path = tmp_path / "udp-with-collisions.pcap"
    packets = rtp_series(3)
    write_udp_capture(
        path,
        packets=packets,
        extra_udp_datagrams=[
            (rtp_packet(sequence=2000, timestamp=100_000), SERVER_RTP, ("192.168.1.99", 60000)),
            (rtp_packet(sequence=2001, timestamp=103_000), ("192.168.1.99", 40000), CLIENT_RTP),
        ],
    )

    source = replay_source.load(path)

    assert [entry.data for entry in source.timeline] == packets


def test_interleaved_capture_round_trips_every_packet(tmp_path: Path) -> None:
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path)
    source = replay_source.load(path)

    assert source.handshake_found
    assert [entry.data for entry in source.timeline] == packets


def test_a_retransmitted_segment_is_not_replayed_twice(tmp_path: Path) -> None:
    path = tmp_path / "retransmit.pcap"
    packets = write_interleaved_capture(path, retransmit_index=4)
    source = replay_source.load(path)
    assert [entry.data for entry in source.timeline] == packets


def test_fmtp_from_the_capture_survives_into_the_served_sdp(tmp_path: Path) -> None:
    path = tmp_path / "udp.pcap"
    write_udp_capture(path)
    text = replay_source.load(path).sdp_text()
    assert "sprop-parameter-sets=Z0LgHtoCgPRA,aM48gA==" in text


def test_timeline_offsets_follow_capture_timing(tmp_path: Path) -> None:
    path = tmp_path / "udp.pcap"
    write_udp_capture(path, packets=rtp_series(5), interval=0.1)
    source = replay_source.load(path)
    offsets = [entry.offset for entry in source.timeline]
    assert offsets == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4], abs=1e-4)
    assert source.duration == pytest.approx(0.4, abs=1e-4)


def test_loop_period_adds_one_mean_gap(tmp_path: Path) -> None:
    """Restarting exactly at `duration` would double up the first packet."""
    path = tmp_path / "udp.pcap"
    write_udp_capture(path, packets=rtp_series(5), interval=0.1)
    source = replay_source.load(path)
    assert source.loop_period() == pytest.approx(0.5, abs=1e-4)


def test_ssrc_span_covers_lost_packets(tmp_path: Path) -> None:
    """A gap in the captured sequence numbers still advances the loop step."""
    packets = [
        rtp_packet(sequence=seq, timestamp=90000 + index * 3000)
        for index, seq in enumerate((100, 101, 105, 106))
    ]
    path = tmp_path / "lossy.pcap"
    write_udp_capture(path, packets=packets)
    track = replay_source.load(path).tracks[0]
    (span,) = track.ssrc_spans.values()
    assert span.span == 7  # 100 → 106 inclusive, not the four packets present


# -- fallbacks --------------------------------------------------------------


def test_ssrc_span_measures_the_rtp_clock_extent(tmp_path: Path) -> None:
    """The loop step must come from the RTP clock, not from wall-clock time.

    Cameras whose RTP clock runs ahead of real time would otherwise get a step
    that is too small, restarting the next loop behind the previous one.
    """
    packets = rtp_series(4, first_timestamp=90000, timestamp_step=3000)
    path = tmp_path / "fast-clock.pcap"
    write_udp_capture(path, packets=packets, interval=0.005)
    (span,) = replay_source.load(path).tracks[0].ssrc_spans.values()

    assert span.timestamp_span == 9000
    assert span.timestamp_steps == 3
    assert span.timestamp_step() == 12000  # 9000 covered + one 3000 increment


def test_fragments_of_one_frame_do_not_inflate_the_timestamp_step(tmp_path: Path) -> None:
    """FU-A fragments share a timestamp; counting them would shrink the step."""
    packets = [
        rtp_packet(sequence=100 + index, timestamp=90000 + (index // 3) * 3000)
        for index in range(6)
    ]
    path = tmp_path / "fragmented.pcap"
    write_udp_capture(path, packets=packets, interval=0.005)
    (span,) = replay_source.load(path).tracks[0].ssrc_spans.values()

    assert span.timestamp_steps == 1  # two frames, one advance
    assert span.timestamp_step() == 6000


def test_capture_without_a_handshake_falls_back_to_heuristics(tmp_path: Path) -> None:
    path = tmp_path / "raw.pcap"
    packets = write_udp_capture(path, with_handshake=False)
    source = replay_source.load(path)

    assert not source.handshake_found
    assert [entry.data for entry in source.timeline] == packets
    assert any("no usable RTSP handshake" in warning for warning in source.warnings)


def test_heuristics_separate_streams_by_ssrc(tmp_path: Path) -> None:
    video = rtp_series(6, ssrc=0x1111)
    audio = rtp_series(2, ssrc=0x2222, payload_type=97)
    path = tmp_path / "two.pcap"
    with PcapWriter(path) as writer:
        for index, packet in enumerate(video + audio):
            writer.write_udp(packet, 100.0 + index * 0.01, src=SERVER_RTP, dst=CLIENT_RTP)

    source = replay_source.load(path)
    # Largest stream first, so the video track is index 0.
    assert [len(track.packets) for track in source.tracks] == [6, 2]


def test_sdp_override_replaces_the_captured_description(tmp_path: Path) -> None:
    override = tmp_path / "override.sdp"
    override.write_text(SDP_TEXT.replace("H264/90000", "H265/90000"), encoding="utf-8")
    path = tmp_path / "raw.pcap"
    write_udp_capture(path, with_handshake=False)

    source = replay_source.load(path, sdp_override=override)
    assert "H265/90000" in source.sdp_text()
    assert source.timeline


def test_heuristics_do_not_mutate_the_supplied_description() -> None:
    """The --sdp description belongs to the caller, so it must survive the call.

    The capture here carries one stream while the description names two, which
    is the shape that used to strip the surplus media block off the operator's
    own object as a side effect of building the reply.
    """
    supplied = sdp.parse(SDP_TEXT + "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 opus/48000/2\r\n")
    before = [list(media.lines) for media in supplied.media]
    datagrams = [
        Datagram(
            ts=100.0 + index * 0.01,
            proto="udp",
            src=SERVER_RTP,
            dst=CLIENT_RTP,
            payload=packet,
        )
        for index, packet in enumerate(rtp_series(3, ssrc=0x1111))
    ]

    _, description = _heuristic_tracks(datagrams, [], supplied)

    assert [list(media.lines) for media in supplied.media] == before
    assert description is not supplied


def test_missing_sdp_override_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "udp.pcap"
    write_udp_capture(path)
    with pytest.raises(ReplaySourceError, match="SDP override file not found"):
        replay_source.load(path, sdp_override=tmp_path / "absent.sdp")


def test_capture_without_any_rtp_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "noise.pcap"
    with PcapWriter(path) as writer:
        writer.write_udp(b"just some bytes", 1.0, src=SERVER_RTP, dst=CLIENT_RTP)
    with pytest.raises(ReplaySourceError, match="no RTP stream"):
        replay_source.load(path)


def test_empty_capture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.pcap"
    PcapWriter(path).close()
    with pytest.raises(ReplaySourceError, match="no UDP or TCP payloads"):
        replay_source.load(path)


# -- security ---------------------------------------------------------------


def test_credentials_in_the_capture_raise_a_warning(tmp_path: Path) -> None:
    """SEC-001: a capture carrying auth must not be shared unredacted."""
    path = tmp_path / "auth.pcap"
    packets = rtp_series(4)
    write_udp_capture(path, packets=packets)

    # Rewrite the capture with an Authorization header in the DESCRIBE.
    from captures import CLIENT, SERVER  # local import keeps the fixture list short

    with PcapWriter(path) as writer:
        request = (
            b"DESCRIBE rtsp://cam/stream RTSP/1.0\r\nCSeq: 1\r\n"
            b"Authorization: Basic YWRtaW46aHVudGVyMg==\r\n\r\n"
        )
        response = (
            b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n"
            b"Content-Length: " + str(len(SDP_TEXT)).encode() + b"\r\n\r\n" + SDP_TEXT.encode()
        )
        writer.write_tcp(request, 1.0, src=CLIENT, dst=SERVER, seq=1)
        writer.write_tcp(response, 1.0, src=SERVER, dst=CLIENT, seq=1)
        for index, packet in enumerate(packets):
            writer.write_udp(packet, 2.0 + index * 0.04, src=SERVER_RTP, dst=CLIENT_RTP)

    source = replay_source.load(path)
    assert any("credentials" in warning for warning in source.warnings)


def test_describe_summarises_the_capture(tmp_path: Path) -> None:
    path = tmp_path / "udp.pcap"
    write_udp_capture(path)
    text = replay_source.describe(replay_source.load(path))
    assert "packets:   10" in text
    assert "0xdeadbeef" in text


# -- resource guards --------------------------------------------------------


def test_head_reads_the_start_without_reassembling_everything() -> None:
    stream = TcpStream()
    stream.add(1, b"OPTIONS * RTSP/1.0\r\n", 1.0)
    stream.add(21, b"X" * 10_000, 2.0)

    assert stream.head(7) == b"OPTIONS"
    assert len(stream.head(64)) == 64


def test_head_of_an_empty_stream_is_empty() -> None:
    assert TcpStream().head(16) == b""


def test_an_oversized_capture_is_refused_with_a_way_out(tmp_path: Path, monkeypatch) -> None:
    """A clear refusal beats an OOM kill with no diagnostic."""
    path = tmp_path / "huge.pcap"
    write_udp_capture(path)
    monkeypatch.setenv(MAX_CAPTURE_BYTES_ENV, "16")

    with pytest.raises(ReplaySourceError, match=MAX_CAPTURE_BYTES_ENV):
        replay_source.load(path)

    # The limit is an env var precisely so a legitimate big capture can pass.
    monkeypatch.setenv(MAX_CAPTURE_BYTES_ENV, str(10 * 1024 * 1024))
    assert replay_source.load(path).packet_count == 10


def test_the_refusal_states_the_memory_a_load_would_need(tmp_path: Path, monkeypatch) -> None:
    """The file size alone does not tell you why 300 MB is a problem.

    Loading costs ~6x the file size in RAM (measured from 2.7 MB to 164 MB),
    so the number that actually decides whether a capture is viable is the
    projected memory, not the size on disk.
    """
    path = tmp_path / "huge.pcap"
    write_udp_capture(path)
    monkeypatch.setenv(MAX_CAPTURE_BYTES_ENV, "16")

    with pytest.raises(ReplaySourceError, match=r"GB of memory"):
        replay_source.load(path)
