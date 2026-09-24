"""Builders for synthetic RTSP/RTP captures used across the replay tests.

Real camera captures cannot be committed (they are large, and they carry
credentials), so the tests synthesise the two shapes that matter: RTP over UDP
negotiated by an RTSP handshake, and RTP interleaved into the RTSP connection
itself.
"""

from __future__ import annotations

import struct
from pathlib import Path

from vcam.pcap import PcapWriter

CLIENT = ("192.168.1.50", 51000)
SERVER = ("192.168.1.10", 554)
SERVER_RTP = ("192.168.1.10", 40000)
CLIENT_RTP = ("192.168.1.50", 60000)

SDP_TEXT = (
    "v=0\r\n"
    "o=- 123 1 IN IP4 192.168.1.10\r\n"
    "s=Camera\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "t=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1;sprop-parameter-sets=Z0LgHtoCgPRA,aM48gA==\r\n"
    "a=control:trackID=0\r\n"
)


def rtp_packet(
    *,
    sequence: int,
    timestamp: int,
    ssrc: int = 0xDEADBEEF,
    payload_type: int = 96,
    marker: bool = False,
    payload: bytes = b"",
) -> bytes:
    """A well-formed RTP packet with a deterministic payload."""
    first = 0x80
    second = (0x80 if marker else 0) | (payload_type & 0x7F)
    header = struct.pack("!BBHII", first, second, sequence & 0xFFFF, timestamp, ssrc)
    return header + (payload or bytes([sequence & 0xFF]) * 40)


def rtp_series(
    count: int,
    *,
    ssrc: int = 0xDEADBEEF,
    first_sequence: int = 1000,
    first_timestamp: int = 90000,
    timestamp_step: int = 3000,
    payload_type: int = 96,
) -> list[bytes]:
    return [
        rtp_packet(
            sequence=first_sequence + index,
            timestamp=first_timestamp + index * timestamp_step,
            ssrc=ssrc,
            payload_type=payload_type,
            marker=index % 3 == 2,
        )
        for index in range(count)
    ]


def _request(method: str, uri: str, cseq: int, extra: str = "") -> bytes:
    return f"{method} {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n{extra}\r\n".encode()


def _response(cseq: int, extra: str = "", body: bytes = b"") -> bytes:
    head = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}"
    if body:
        head += f"Content-Length: {len(body)}\r\n"
    return head.encode() + b"\r\n" + body


def _interleaved(channel: int, payload: bytes) -> bytes:
    return b"$" + bytes([channel]) + struct.pack("!H", len(payload)) + payload


class _TcpSide:
    """Tracks a TCP sequence number so segments reassemble in order."""

    def __init__(self, src: tuple[str, int], dst: tuple[str, int]) -> None:
        self.src = src
        self.dst = dst
        self.seq = 1

    def emit(self, writer: PcapWriter, payload: bytes, ts: float) -> int:
        seq = self.seq
        writer.write_tcp(payload, ts, src=self.src, dst=self.dst, seq=seq)
        self.seq += len(payload)
        return seq

    def retransmit(self, writer: PcapWriter, payload: bytes, seq: int, ts: float) -> None:
        writer.write_tcp(payload, ts, src=self.src, dst=self.dst, seq=seq)


def write_udp_capture(
    path: Path,
    *,
    packets: list[bytes] | None = None,
    interval: float = 0.04,
    start: float = 1_700_000_000.0,
    with_handshake: bool = True,
    extra_udp_datagrams: list[tuple[bytes, tuple[str, int], tuple[str, int]]] | None = None,
) -> list[bytes]:
    """RTSP handshake negotiating RTP/AVP over UDP, followed by the RTP stream."""
    packets = packets if packets is not None else rtp_series(10)
    client = _TcpSide(CLIENT, SERVER)
    server = _TcpSide(SERVER, CLIENT)

    with PcapWriter(path) as writer:
        ts = start
        if with_handshake:
            client.emit(writer, _request("OPTIONS", "rtsp://cam/stream", 1), ts)
            server.emit(writer, _response(1, "Public: DESCRIBE, SETUP, PLAY\r\n"), ts)
            client.emit(
                writer,
                _request("DESCRIBE", "rtsp://cam/stream", 2, "Accept: application/sdp\r\n"),
                ts,
            )
            server.emit(
                writer,
                _response(2, "Content-Type: application/sdp\r\n", SDP_TEXT.encode()),
                ts,
            )
            client.emit(
                writer,
                _request(
                    "SETUP",
                    "rtsp://cam/stream/trackID=0",
                    3,
                    "Transport: RTP/AVP;unicast;"
                    f"client_port={CLIENT_RTP[1]}-{CLIENT_RTP[1] + 1}\r\n",
                ),
                ts,
            )
            server.emit(
                writer,
                _response(
                    3,
                    "Transport: RTP/AVP;unicast;"
                    f"client_port={CLIENT_RTP[1]}-{CLIENT_RTP[1] + 1};"
                    f"server_port={SERVER_RTP[1]}-{SERVER_RTP[1] + 1}\r\n"
                    "Session: 12345678\r\n",
                ),
                ts,
            )
            client.emit(
                writer, _request("PLAY", "rtsp://cam/stream", 4, "Session: 12345678\r\n"), ts
            )
            server.emit(writer, _response(4, "Session: 12345678\r\n"), ts)

        for index, packet in enumerate(packets):
            writer.write_udp(packet, start + index * interval, src=SERVER_RTP, dst=CLIENT_RTP)
        for index, (packet, src, dst) in enumerate(extra_udp_datagrams or []):
            writer.write_udp(
                packet,
                start + (len(packets) + index) * interval,
                src=src,
                dst=dst,
            )
    return packets


def write_interleaved_capture(
    path: Path,
    *,
    packets: list[bytes] | None = None,
    interval: float = 0.04,
    start: float = 1_700_000_000.0,
    retransmit_index: int | None = None,
) -> list[bytes]:
    """RTSP over TCP with RTP interleaved on channel 0.

    ``retransmit_index`` re-sends one segment with its original sequence
    number, which is what a lossy capture looks like and what reassembly has
    to collapse back to a single copy.
    """
    packets = packets if packets is not None else rtp_series(10)
    client = _TcpSide(CLIENT, SERVER)
    server = _TcpSide(SERVER, CLIENT)

    with PcapWriter(path) as writer:
        ts = start
        client.emit(writer, _request("OPTIONS", "rtsp://cam/stream", 1), ts)
        server.emit(writer, _response(1, "Public: DESCRIBE, SETUP, PLAY\r\n"), ts)
        client.emit(
            writer, _request("DESCRIBE", "rtsp://cam/stream", 2, "Accept: application/sdp\r\n"), ts
        )
        server.emit(
            writer, _response(2, "Content-Type: application/sdp\r\n", SDP_TEXT.encode()), ts
        )
        client.emit(
            writer,
            _request(
                "SETUP",
                "rtsp://cam/stream/trackID=0",
                3,
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
            ),
            ts,
        )
        server.emit(
            writer,
            _response(
                3,
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: 12345678\r\n",
            ),
            ts,
        )
        client.emit(writer, _request("PLAY", "rtsp://cam/stream", 4, "Session: 12345678\r\n"), ts)
        server.emit(writer, _response(4, "Session: 12345678\r\n"), ts)

        for index, packet in enumerate(packets):
            frame = _interleaved(0, packet)
            when = start + index * interval
            seq = server.emit(writer, frame, when)
            if retransmit_index is not None and index == retransmit_index:
                server.retransmit(writer, frame, seq, when + interval / 4)

    return packets
