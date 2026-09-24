"""Turn a capture file into a replayable RTSP session.

Two things make this harder than "read the UDP packets":

* RTP ports are negotiated per session in ``SETUP``; there is no well-known
  port to filter on.
* Most IP cameras negotiate ``RTP/AVP/TCP``, so the RTP lives ``$``-framed
  inside the RTSP TCP connection and only appears after stream reassembly.

Both cases are handled here, with a last-resort heuristic for captures that
started after the handshake.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import rtp, sdp
from .errors import ReplaySourceError
from .pcap import Datagram, PcapError, iter_datagrams
from .rtsp_messages import (
    InterleavedFrame,
    RtspFramingError,
    RtspMessage,
    RtspStreamParser,
    StreamItem,
    parse_port_pair,
    parse_transport,
)

logger = logging.getLogger("vcam")

Endpoint = tuple[str, int]
FlowKey = tuple[Endpoint, Endpoint]

#: Loading materialises the capture several times over (datagrams, reassembled
#: TCP streams, the timeline), so refuse a file that would exhaust memory
#: instead of being OOM-killed with no explanation.
#:
#: Measured across five captures from 2.7 MB to 164 MB, the cost is linear and
#: converges to ~6x the file size in RSS at ~15 MB/s. The default therefore
#: caps a load at roughly 1.5 GB of peak memory; raise
#: ``VCAM_MAX_CAPTURE_BYTES`` if you have the headroom and the patience.
LOAD_MEMORY_FACTOR = 6
DEFAULT_MAX_CAPTURE_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_BYTES_ENV = "VCAM_MAX_CAPTURE_BYTES"

#: Credentials that survive into a capture verbatim (SEC-001).
_CREDENTIAL_HEADERS = ("authorization", "www-authenticate", "proxy-authorization")
_URI_CREDENTIALS_RE = re.compile(r"rtsps?://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RtpRecord:
    ts: float
    data: bytes


@dataclass
class SsrcSpan:
    """Extent of one SSRC across the capture, used to keep loops monotonic."""

    first_sequence: int
    last_sequence: int
    first_timestamp: int = 0
    last_timestamp: int = 0
    timestamp_steps: int = 0
    """Number of times the RTP timestamp advanced, i.e. distinct frames - 1."""

    @property
    def span(self) -> int:
        return rtp.sequence_span(self.first_sequence, self.last_sequence)

    @property
    def timestamp_span(self) -> int:
        """RTP-clock distance from the first packet to the last, honouring wrap."""
        return (self.last_timestamp - self.first_timestamp) % rtp.TIMESTAMP_MODULO

    def timestamp_step(self) -> int:
        """Timestamp increment for one loop, measured on the RTP clock itself.

        Deriving this from wall-clock time alone breaks whenever a camera's RTP
        clock does not match real time — the step comes out short and the next
        loop restarts *behind* the previous one, which is exactly the backwards
        discontinuity looping is supposed to avoid.
        """
        span = self.timestamp_span
        if span <= 0 or self.timestamp_steps <= 0:
            return 0
        return span + round(span / self.timestamp_steps)


@dataclass
class ReplayTrack:
    index: int
    media: sdp.MediaDescription
    packets: list[RtpRecord] = field(default_factory=list)
    payload_type: int = 96
    clock_rate: int = sdp.DEFAULT_CLOCK_RATE
    ssrc_spans: dict[int, SsrcSpan] = field(default_factory=dict)

    @property
    def media_type(self) -> str:
        return self.media.media_type

    @property
    def control(self) -> str:
        return f"trackID={self.index}"


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    offset: float
    """Seconds since the first packet of the capture."""
    track_index: int
    data: bytes


@dataclass
class ReplaySource:
    """Plays back a captured RTSP session verbatim from a PCAP file.

    Holds the timeline of all packets in the capture, handles looping
    (with optional RTP sequence/timestamp rewriting to keep streams monotonic),
    and provides track metadata for clients to negotiate.

    Attributes:
        path: Path to the .pcap/.pcapng capture file.
        loop: If True, restart playback from the beginning when reaching the end.
        speed: Playback speed multiplier (1.0 = real-time).
        rewrite_on_loop: If True, increment RTP sequence numbers and timestamps
            across loops to maintain monotonicity. Disable only to reproduce
            raw, discontinuous looping for testing edge cases.
        tracks: List of ReplayTrack objects, one per media stream in the capture.
        session_description: The SDP session description from the capture or override.
        timeline: List of TimelineEntry objects mapping capture time to packets.
    """

    path: Path
    description: sdp.SessionDescription
    tracks: list[ReplayTrack]
    timeline: list[TimelineEntry]
    duration: float
    warnings: list[str] = field(default_factory=list)
    handshake_found: bool = False

    def sdp_text(self, *, connection_address: str = "0.0.0.0") -> str:
        return sdp.render(
            self.description,
            connection_address=connection_address,
            duration=self.duration or None,
        )

    @property
    def packet_count(self) -> int:
        return len(self.timeline)

    def loop_period(self) -> float:
        """Wall-clock length of one loop, including the gap before restarting.

        Restarting exactly at ``duration`` would place the first packet of the
        next loop at the same instant as the last of this one; padding by the
        mean inter-packet gap keeps the cadence believable.
        """
        if self.packet_count < 2 or self.duration <= 0:
            return max(self.duration, 0.0)
        return self.duration + self.duration / (self.packet_count - 1)


# ---------------------------------------------------------------------------
# TCP reassembly
# ---------------------------------------------------------------------------


class TcpStream:
    """Reassemble one direction of a TCP connection, keeping packet times."""

    def __init__(self) -> None:
        self._segments: dict[int, tuple[bytes, float]] = {}

    def add(self, sequence: int, payload: bytes, ts: float) -> None:
        existing = self._segments.get(sequence)
        # A retransmission carries the same or less data; keep the longest.
        if existing is not None and len(existing[0]) >= len(payload):
            return
        self._segments[sequence] = (payload, ts)

    def head(self, limit: int) -> bytes:
        """First *limit* bytes of the stream, without reassembling all of it.

        Identifying which flow carries RTSP only needs the opening bytes, and a
        capture may hold many unrelated TCP flows whose full reassembly would
        cost more memory than the replay itself.
        """
        data = bytearray()
        for _, (payload, _ts) in sorted(self._segments.items()):
            data.extend(payload)
            if len(data) >= limit:
                break
        return bytes(data[:limit])

    def assemble(self) -> tuple[bytes, list[tuple[int, float]]]:
        """Return ``(stream_bytes, [(offset, ts), ...])`` ordered by sequence."""
        if not self._segments:
            return b"", []

        ordered = sorted(self._segments.items())
        base = ordered[0][0]
        data = bytearray()
        marks: list[tuple[int, float]] = []

        for sequence, (payload, ts) in ordered:
            offset = sequence - base
            if offset < 0:
                continue
            if offset < len(data):
                overlap = len(data) - offset
                if overlap >= len(payload):
                    continue  # fully retransmitted
                payload = payload[overlap:]
                offset = len(data)
            elif offset > len(data):
                # Missing segment: pad so later offsets stay correct. The hole
                # will simply fail to parse as a frame, which is the honest
                # outcome for a capture with dropped packets.
                data.extend(bytes(offset - len(data)))
            marks.append((offset, ts))
            data.extend(payload)

        return bytes(data), marks


class StreamClock:
    """Map a byte offset in a reassembled stream back to a capture time."""

    def __init__(self, marks: list[tuple[int, float]]) -> None:
        self._offsets = [offset for offset, _ in marks]
        self._times = [ts for _, ts in marks]

    def at(self, offset: int) -> float:
        if not self._offsets:
            return 0.0
        position = bisect.bisect_right(self._offsets, offset) - 1
        return self._times[max(position, 0)]


# ---------------------------------------------------------------------------
# Capture analysis
# ---------------------------------------------------------------------------


@dataclass
class _Handshake:
    """What the recorded RTSP exchange told us about the session."""

    sdp_text: str | None = None
    server: Endpoint | None = None
    client: Endpoint | None = None
    interleaved: dict[str, int] = field(default_factory=dict)
    """Control URL → RTP channel."""
    udp_ports: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)
    """Control URL → (server RTP port, client RTP port)."""
    setup_order: list[str] = field(default_factory=list)
    credentials_seen: bool = False


def _collect_tcp_streams(datagrams: Iterable[Datagram]) -> dict[FlowKey, TcpStream]:
    streams: dict[FlowKey, TcpStream] = {}
    for datagram in datagrams:
        if datagram.proto != "tcp" or datagram.tcp_seq is None:
            continue
        key = (datagram.src, datagram.dst)
        streams.setdefault(key, TcpStream()).add(datagram.tcp_seq, datagram.payload, datagram.ts)
    return streams


def _find_rtsp_flow(streams: dict[FlowKey, TcpStream]) -> FlowKey | None:
    """Return the client→server flow of the first RTSP session in the capture."""
    for key, stream in streams.items():
        head = stream.head(4096)
        if b"RTSP/1.0" in head and re.match(rb"[A-Z_]+ \S+ RTSP/1\.0", head):
            return key
    return None


def _message_credentials(message: RtspMessage) -> bool:
    if any(header in message.headers for header in _CREDENTIAL_HEADERS):
        return True
    return bool(message.uri and _URI_CREDENTIALS_RE.search(message.uri))


def _analyse_handshake(
    requests: list[RtspMessage],
    responses: dict[int, RtspMessage],
    client: Endpoint,
    server: Endpoint,
) -> _Handshake:
    handshake = _Handshake(client=client, server=server)

    for request in requests:
        if _message_credentials(request):
            handshake.credentials_seen = True
        cseq = request.cseq
        if cseq is None:
            continue
        response = responses.get(cseq)
        if response is None or (response.status or 0) >= 300:
            continue
        if _message_credentials(response):
            handshake.credentials_seen = True

        method = request.method
        if method == "DESCRIBE" and handshake.sdp_text is None:
            if "application/sdp" in response.headers.get("content-type", "").lower():
                handshake.sdp_text = response.body.decode("utf-8", errors="replace")
        elif method == "SETUP":
            control = request.uri or ""
            handshake.setup_order.append(control)
            transport = parse_transport(response.headers.get("transport", ""))
            interleaved = parse_port_pair(transport.get("interleaved", ""))
            if interleaved is not None:
                handshake.interleaved[control] = interleaved[0]
                continue
            server_ports = parse_port_pair(transport.get("server_port", ""))
            client_request = parse_transport(request.headers.get("transport", ""))
            client_ports = parse_port_pair(
                transport.get("client_port", "") or client_request.get("client_port", "")
            )
            handshake.udp_ports[control] = (
                server_ports[0] if server_ports else None,
                client_ports[0] if client_ports else None,
            )

    return handshake


def _match_control(control_url: str, tracks: list[ReplayTrack]) -> int | None:
    """Map a ``SETUP`` request URI onto the media block it set up."""
    for track in tracks:
        control = track.media.control
        if control and (control_url.endswith(control) or control == control_url):
            return track.index
    return None


def _register_packet(track: ReplayTrack, ts: float, data: bytes) -> None:
    header = rtp.parse(data)
    if header is None:
        return
    track.packets.append(RtpRecord(ts=ts, data=data))
    span = track.ssrc_spans.get(header.ssrc)
    if span is None:
        track.ssrc_spans[header.ssrc] = SsrcSpan(
            first_sequence=header.sequence,
            last_sequence=header.sequence,
            first_timestamp=header.timestamp,
            last_timestamp=header.timestamp,
        )
        return
    span.last_sequence = header.sequence
    if header.timestamp != span.last_timestamp:
        # Fragments of one frame share a timestamp; only count real advances.
        span.timestamp_steps += 1
        span.last_timestamp = header.timestamp


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _max_capture_bytes() -> int:
    raw = os.environ.get(MAX_CAPTURE_BYTES_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_MAX_CAPTURE_BYTES


def _check_capture_size(capture: Path) -> None:
    """Refuse a capture large enough to exhaust memory during loading."""
    try:
        size = capture.stat().st_size
    except OSError:
        return  # iter_datagrams reports a missing or unreadable file properly
    limit = _max_capture_bytes()
    if size > limit:
        raise ReplaySourceError(
            f"{capture} is {size / 1e6:.0f} MB, over the {limit / 1e6:.0f} MB replay "
            f"limit; loading it would need roughly {size * LOAD_MEMORY_FACTOR / 1e9:.1f} GB "
            "of memory. Trim it (`editcap -A/-B`, or a tighter tcpdump filter) or raise "
            f"${MAX_CAPTURE_BYTES_ENV}"
        )


def load(
    path: Path | str,
    *,
    sdp_override: Path | None = None,
) -> ReplaySource:
    """Build a :class:`ReplaySource` from the capture at *path*."""
    capture = Path(path).expanduser()
    _check_capture_size(capture)
    datagrams = list(iter_datagrams(capture))
    if not datagrams:
        raise ReplaySourceError(f"{capture} contains no UDP or TCP payloads")

    warnings: list[str] = []
    streams = _collect_tcp_streams(datagrams)
    handshake, interleaved_frames, clock = _read_handshake(streams)

    if handshake.credentials_seen:
        warnings.append(
            "the capture contains RTSP credentials; redact it before sharing "
            "(`editcap -E 0 in.pcap out.pcap` or strip the DESCRIBE exchange)"
        )

    description = _load_description(handshake, sdp_override, warnings)
    tracks = [
        ReplayTrack(index=index, media=media) for index, media in enumerate(description.media)
    ]

    if tracks:
        _fill_tracks(tracks, handshake, interleaved_frames, clock, datagrams)

    if not tracks or not any(track.packets for track in tracks):
        tracks, description = _heuristic_tracks(
            datagrams, warnings, description if sdp_override is not None else None
        )

    if not tracks:
        raise ReplaySourceError(
            f"{capture} contains no RTP stream that could be identified; "
            "capture the RTSP handshake as well, or pass --sdp with a description"
        )

    for track in tracks:
        header = rtp.parse(track.packets[0].data) if track.packets else None
        if header is not None:
            track.payload_type = header.payload_type
        track.clock_rate = track.media.clock_rate(track.payload_type)

    timeline, duration = _build_timeline(tracks)
    return ReplaySource(
        path=capture,
        description=description,
        tracks=tracks,
        timeline=timeline,
        duration=duration,
        warnings=warnings,
        handshake_found=handshake.sdp_text is not None,
    )


def _parse_stream(data: bytes) -> list[StreamItem]:
    """Parse one direction of a stream, stopping at the first framing error.

    A truncated or noisy capture is normal; whatever was recovered before the
    stream stopped making sense is still worth replaying.
    """
    items: list[StreamItem] = []
    try:
        items.extend(RtspStreamParser().feed(data))
    except RtspFramingError as exc:
        logger.debug("replay: stopped parsing RTSP stream: %s", exc)
    return items


def _read_handshake(
    streams: dict[FlowKey, TcpStream],
) -> tuple[_Handshake, list[tuple[float, InterleavedFrame]], StreamClock]:
    """Locate and parse the RTSP exchange, plus any interleaved data with it."""
    empty_clock = StreamClock([])
    client_flow = _find_rtsp_flow(streams)
    if client_flow is None:
        return _Handshake(), [], empty_clock

    client, server = client_flow
    request_data, _ = streams[client_flow].assemble()
    requests = [item for item in _parse_stream(request_data) if isinstance(item, RtspMessage)]

    server_flow = (server, client)
    responses: dict[int, RtspMessage] = {}
    frames: list[tuple[float, InterleavedFrame]] = []
    clock = empty_clock

    if server_flow in streams:
        response_data, marks = streams[server_flow].assemble()
        clock = StreamClock(marks)
        for item in _parse_stream(response_data):
            if isinstance(item, RtspMessage):
                cseq = item.cseq
                if cseq is not None:
                    responses.setdefault(cseq, item)
            else:
                frames.append((clock.at(item.offset), item))

    return _analyse_handshake(requests, responses, client, server), frames, clock


def _load_description(
    handshake: _Handshake,
    sdp_override: Path | None,
    warnings: list[str],
) -> sdp.SessionDescription:
    if sdp_override is not None:
        override = Path(sdp_override).expanduser()
        if not override.is_file():
            raise ReplaySourceError(f"SDP override file not found: {override}")
        return sdp.parse(override.read_text(encoding="utf-8"))
    if handshake.sdp_text:
        return sdp.parse(handshake.sdp_text)
    warnings.append(
        "no RTSP DESCRIBE response in the capture; the session description is "
        "a guess — pass --sdp FILE if the consumer rejects the stream"
    )
    return sdp.SessionDescription()


def _fill_tracks(
    tracks: list[ReplayTrack],
    handshake: _Handshake,
    interleaved_frames: list[tuple[float, InterleavedFrame]],
    clock: StreamClock,
    datagrams: list[Datagram],
) -> None:
    """Attach the captured RTP packets to the track each one belongs to."""
    del clock  # frames already carry their capture time

    for position, control in enumerate(handshake.setup_order):
        index = _match_control(control, tracks)
        if index is None:
            index = position if position < len(tracks) else None
        if index is None:
            continue
        track = tracks[index]

        channel = handshake.interleaved.get(control)
        if channel is not None:
            for ts, frame in interleaved_frames:
                if frame.channel == channel:
                    _register_packet(track, ts, frame.payload)
            continue

        server_port, client_port = handshake.udp_ports.get(control, (None, None))
        if server_port is None and client_port is None:
            continue
        for datagram in datagrams:
            if datagram.proto != "udp":
                continue
            if handshake.server is None or handshake.client is None:
                continue
            if (
                datagram.src[0] != handshake.server[0]
                or datagram.dst[0] != handshake.client[0]
                or (server_port is not None and datagram.src[1] != server_port)
                or (client_port is not None and datagram.dst[1] != client_port)
            ):
                continue
            _register_packet(track, datagram.ts, datagram.payload)


def _heuristic_tracks(
    datagrams: list[Datagram],
    warnings: list[str],
    preferred: sdp.SessionDescription | None = None,
) -> tuple[list[ReplayTrack], sdp.SessionDescription]:
    """Group plausible RTP by (flow, SSRC) when the handshake is unavailable.

    *preferred* is an operator-supplied description: it describes the streams
    authoritatively, so its media blocks are used in order and only the surplus
    streams get a synthesised one.
    """
    groups: dict[tuple[object, int], list[Datagram]] = {}
    for datagram in datagrams:
        if datagram.proto != "udp" or not rtp.looks_like_rtp(datagram.payload):
            continue
        header = rtp.parse(datagram.payload)
        assert header is not None  # looks_like_rtp already parsed it
        groups.setdefault((datagram.flow, header.ssrc), []).append(datagram)

    if not groups:
        return [], preferred or sdp.SessionDescription()

    warnings.append(
        f"no usable RTSP handshake; recovered {len(groups)} RTP stream(s) by "
        "inspecting UDP payloads"
        + ("" if preferred is not None else ", and assumed the codec from the payload type")
    )

    description = sdp.SessionDescription(
        # Built fresh rather than reusing *preferred*. Clearing and refilling
        # the caller's description mutated the operator's --sdp in place, and
        # when the capture held fewer streams than it described the surplus
        # media blocks were silently dropped from the object they passed in.
        session_lines=list(preferred.session_lines) if preferred is not None else []
    )
    supplied = list(preferred.media) if preferred is not None else []
    tracks: list[ReplayTrack] = []
    # Largest stream first: video before the audio or metadata side-channels.
    ordered = sorted(groups.values(), key=len, reverse=True)
    for index, packets in enumerate(ordered):
        header = rtp.parse(packets[0].payload)
        assert header is not None
        media = (
            supplied[index] if index < len(supplied) else sdp.synthetic_media(header.payload_type)
        )
        description.media.append(media)
        track = ReplayTrack(index=index, media=media)
        for datagram in packets:
            _register_packet(track, datagram.ts, datagram.payload)
        tracks.append(track)

    return tracks, description


def _build_timeline(tracks: list[ReplayTrack]) -> tuple[list[TimelineEntry], float]:
    """Merge every track into one capture-ordered playback timeline."""
    merged: list[tuple[float, int, bytes]] = [
        (record.ts, track.index, record.data) for track in tracks for record in track.packets
    ]
    if not merged:
        return [], 0.0

    merged.sort(key=lambda item: item[0])
    start = merged[0][0]
    timeline = [
        TimelineEntry(offset=ts - start, track_index=index, data=data) for ts, index, data in merged
    ]
    return timeline, timeline[-1].offset


def describe(source: ReplaySource) -> str:
    """Human-readable summary used by ``vcam replay --list-tracks``."""
    lines = [
        f"capture:   {source.path}",
        f"duration:  {source.duration:.3f}s",
        f"packets:   {source.packet_count}",
        f"handshake: {'yes' if source.handshake_found else 'no (guessed SDP)'}",
        "tracks:",
    ]
    for track in source.tracks:
        lines.append(
            f"  [{track.index}] {track.media_type} pt={track.payload_type} "
            f"clock={track.clock_rate} packets={len(track.packets)} "
            f"ssrc={', '.join(f'0x{value:08x}' for value in track.ssrc_spans)}"
        )
    for warning in source.warnings:
        lines.append(f"warning:   {warning}")
    return "\n".join(lines)


__all__ = [
    "PcapError",
    "ReplaySource",
    "ReplaySourceError",
    "ReplayTrack",
    "RtpRecord",
    "TcpStream",
    "TimelineEntry",
    "describe",
    "load",
]
