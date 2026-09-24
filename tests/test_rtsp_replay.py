"""End-to-end replay over RTSP (TEST-010…TEST-013).

These tests drive a real RTSP client against a real server socket, because the
value of this feature is entirely in what appears on the wire.
"""

from __future__ import annotations

import base64
import logging
import socket
import struct
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from captures import rtp_series, write_interleaved_capture, write_udp_capture
from vcam import replay_source, rtp, rtsp_replay
from vcam.rtsp_messages import InterleavedFrame, RtspMessage, RtspStreamParser
from vcam.rtsp_replay import ReplayServer


class RtspClient:
    """A small blocking RTSP client: enough to exercise the whole state machine."""

    def __init__(self, host: str, port: int, path: str, credentials: str | None = None) -> None:
        self.url = f"rtsp://{host}:{port}/{path}"
        self.socket = socket.create_connection((host, port), timeout=5)
        self.socket.settimeout(5)
        self._parser = RtspStreamParser()
        self._pending: list[object] = []
        self._cseq = 0
        self._credentials = credentials
        self.session: str | None = None

    def close(self) -> None:
        self.socket.close()

    # -- protocol ------------------------------------------------------------

    def request(self, method: str, uri: str | None = None, **headers: str) -> RtspMessage:
        self._cseq += 1
        lines = [f"{method} {uri or self.url} RTSP/1.0", f"CSeq: {self._cseq}"]
        if self.session:
            lines.append(f"Session: {self.session}")
        if self._credentials:
            token = base64.b64encode(self._credentials.encode()).decode()
            lines.append(f"Authorization: Basic {token}")
        lines.extend(f"{name.replace('_', '-')}: {value}" for name, value in headers.items())
        self.socket.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        response = self._next(RtspMessage)
        session = response.headers.get("session")
        if session:
            self.session = session.split(";")[0].strip()
        return response

    def _next(self, kind: type) -> object:
        while True:
            for index, item in enumerate(self._pending):
                if isinstance(item, kind):
                    return self._pending.pop(index)
            chunk = self.socket.recv(65536)
            if not chunk:
                raise AssertionError(f"connection closed while waiting for {kind.__name__}")
            self._pending.extend(self._parser.feed(chunk))

    def interleaved(self, count: int, channel: int = 0) -> list[bytes]:
        """Collect *count* RTP packets from the interleaved channel."""
        frames: list[bytes] = []
        while len(frames) < count:
            frame = self._next(InterleavedFrame)
            assert isinstance(frame, InterleavedFrame)
            if frame.channel == channel:
                frames.append(frame.payload)
        return frames


@pytest.fixture
def udp_capture(tmp_path: Path) -> tuple[Path, list[bytes]]:
    path = tmp_path / "udp.pcap"
    packets = write_udp_capture(path, packets=rtp_series(6), interval=0.01)
    return path, packets


@pytest.fixture
def server_factory() -> Iterator[object]:
    servers: list[ReplayServer] = []

    def make(source_path: Path, **kwargs: object) -> ReplayServer:
        source = replay_source.load(source_path)
        server = ReplayServer(source, host="127.0.0.1", port=0, path="replay", **kwargs)  # type: ignore[arg-type]
        server.start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.shutdown()


def _drain_udp(sock: socket.socket, count: int, timeout: float = 5.0) -> list[bytes]:
    deadline = time.monotonic() + timeout
    packets: list[bytes] = []
    sock.settimeout(0.5)
    while len(packets) < count and time.monotonic() < deadline:
        try:
            packets.append(sock.recv(65536))
        except TimeoutError:
            continue
    return packets


# -- basic protocol ---------------------------------------------------------


def test_options_advertises_the_methods(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        response = client.request("OPTIONS")
        assert response.status == 200
        assert "PLAY" in response.headers["public"]
        assert "TEARDOWN" in response.headers["public"]
    finally:
        client.close()


def test_describe_returns_the_captured_sdp(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        response = client.request("DESCRIBE", Accept="application/sdp")
        body = response.body.decode()
        assert response.headers["content-type"] == "application/sdp"
        assert "m=video 0 RTP/AVP 96" in body
        assert "sprop-parameter-sets=Z0LgHtoCgPRA,aM48gA==" in body
        assert "a=control:trackID=0" in body
    finally:
        client.close()


def test_describe_on_an_unknown_path_is_404(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        response = client.request("DESCRIBE", f"rtsp://127.0.0.1:{server.port}/other")
        assert response.status == 404
    finally:
        client.close()


def test_setup_on_an_unknown_track_is_404(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        response = client.request(
            "SETUP",
            f"{client.url}/trackID=999",
            Transport="RTP/AVP/TCP;unicast;interleaved=0-1",
        )
        assert response.status == 404
    finally:
        client.close()


def test_unknown_method_is_501(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        assert client.request("RECORD").status == 501
    finally:
        client.close()


def test_play_before_setup_is_rejected(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        assert client.request("PLAY").status == 455
    finally:
        client.close()


def test_setup_without_a_usable_transport_is_461(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        response = client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP;unicast")
        assert response.status == 461
    finally:
        client.close()


# -- interleaved playback ---------------------------------------------------


def test_interleaved_playback_is_byte_for_byte(tmp_path: Path, server_factory) -> None:
    """REQ-003: what the reader gets is exactly what the camera sent."""
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(8), interval=0.01)
    server = server_factory(path, loop=False)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("DESCRIBE", Accept="application/sdp")
        setup = client.request(
            "SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;unicast;interleaved=0-1"
        )
        assert setup.status == 200
        assert "interleaved=0-1" in setup.headers["transport"]
        assert client.session

        play = client.request("PLAY", Range="npt=0.000-")
        assert play.status == 200
        assert f"seq={rtp.parse(packets[0]).sequence}" in play.headers["rtp-info"]

        received = client.interleaved(len(packets))
        assert received == packets
    finally:
        client.close()


def test_teardown_closes_the_session(tmp_path: Path, server_factory) -> None:
    path = tmp_path / "tcp.pcap"
    write_interleaved_capture(path, packets=rtp_series(4), interval=0.01)
    server = server_factory(path)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        assert client.request("TEARDOWN").status == 200
        assert client.socket.recv(4096) == b""  # server closed its end
    finally:
        client.close()


def test_looping_keeps_sequence_numbers_monotonic(tmp_path: Path, server_factory) -> None:
    """A raw rewind stalls decoders; the second loop must continue upwards.

    The capture is deliberately inconsistent — 5 ms between packets on the wire
    but 33 ms of RTP clock per packet — because that mismatch is what breaks a
    loop step derived from wall-clock time alone.
    """
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(4), interval=0.005)
    server = server_factory(path, loop=True)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")
        received = client.interleaved(len(packets) * 2)
    finally:
        client.close()

    headers = [rtp.parse(packet) for packet in received]
    sequences = [header.sequence for header in headers]
    timestamps = [header.timestamp for header in headers]
    assert sequences == sorted(sequences)
    assert timestamps == sorted(timestamps)
    # The first loop is untouched, and the payloads repeat unchanged.
    assert received[: len(packets)] == packets
    assert [packet[12:] for packet in received[len(packets) :]] == [
        packet[12:] for packet in packets
    ]
    # SSRC is preserved across the loop boundary.
    assert {header.ssrc for header in headers} == {rtp.parse(packets[0]).ssrc}


def test_no_rewrite_on_loop_replays_the_raw_rewind(tmp_path: Path, server_factory) -> None:
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(3), interval=0.005)
    server = server_factory(path, loop=True, rewrite_on_loop=False)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")
        received = client.interleaved(len(packets) * 2)
    finally:
        client.close()

    assert received == packets * 2


def test_pause_is_refused_rather_than_silently_rewinding(tmp_path: Path, server_factory) -> None:
    """Resuming would restart the timeline with backwards RTP counters."""
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(200), interval=0.002)
    server = server_factory(path, loop=True)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")
        client.interleaved(2)

        assert client.request("PAUSE").status == 501
        # The stream is untouched: playback carries on from where it was.
        assert client.interleaved(2) == packets[2:4]
    finally:
        client.close()


def test_pause_is_not_advertised(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        assert "PAUSE" not in client.request("OPTIONS").headers["public"]
    finally:
        client.close()


# -- UDP playback -----------------------------------------------------------


def test_udp_playback_is_byte_for_byte(udp_capture, server_factory) -> None:
    path, packets = udp_capture
    server = server_factory(path, loop=False)

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client_port = receiver.getsockname()[1]
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        setup = client.request(
            "SETUP",
            f"{client.url}/trackID=0",
            Transport=f"RTP/AVP;unicast;client_port={client_port}-{client_port + 1}",
        )
        assert setup.status == 200
        assert f"client_port={client_port}-{client_port + 1}" in setup.headers["transport"]
        assert "server_port=" in setup.headers["transport"]

        assert client.request("PLAY").status == 200
        assert _drain_udp(receiver, len(packets)) == packets
    finally:
        client.close()
        receiver.close()


# -- authentication ---------------------------------------------------------


def test_credentials_are_required_when_configured(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path, username="admin", password="hunter2")

    anonymous = RtspClient("127.0.0.1", server.port, "replay")
    try:
        # OPTIONS stays open so a reader can discover the challenge.
        assert anonymous.request("OPTIONS").status == 200
        denied = anonymous.request("DESCRIBE")
        assert denied.status == 401
        assert "Basic" in denied.headers["www-authenticate"]
    finally:
        anonymous.close()

    authorised = RtspClient("127.0.0.1", server.port, "replay", credentials="admin:hunter2")
    try:
        assert authorised.request("DESCRIBE").status == 200
    finally:
        authorised.close()


def test_wrong_credentials_are_refused(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path, username="admin", password="hunter2")
    client = RtspClient("127.0.0.1", server.port, "replay", credentials="admin:wrong")
    try:
        assert client.request("DESCRIBE").status == 401
    finally:
        client.close()


# -- configuration ----------------------------------------------------------


def test_loop_steps_are_not_planned_when_they_cannot_be_used(udp_capture) -> None:
    """A one-shot or raw replay should not walk the timeline at startup."""
    path, _ = udp_capture
    source = replay_source.load(path)

    assert ReplayServer(source, host="127.0.0.1", port=0, loop=False).loop_steps == []
    assert (
        ReplayServer(source, host="127.0.0.1", port=0, loop=True, rewrite_on_loop=False).loop_steps
        == []
    )
    assert ReplayServer(source, host="127.0.0.1", port=0, loop=True).loop_steps


def test_a_replay_the_machine_could_not_pace_is_reported(udp_capture, caplog) -> None:
    """A late-packet count that only ever lands in memory helps nobody.

    Absolute deadlines stop a late packet from dragging the rest of the stream
    with it, so this failure is invisible from the outside: the operator sees a
    stream that is still in sync and has no way to learn their machine was the
    reason the spacing was wrong. The counter is only worth keeping if it is
    said out loud.
    """
    path, _ = udp_capture
    source = replay_source.load(path)
    server = ReplayServer(source, host="127.0.0.1", port=0)
    local, remote = socket.socketpair()
    try:
        connection = rtsp_replay._Connection(local, server, "10.0.0.9:5555")
        player = rtsp_replay._Player(connection)

        with caplog.at_level(logging.WARNING):
            player._report_pacing()
        assert caplog.text == "", "a replay that kept pace should say nothing"

        player._pacer.late_packets = 3
        with caplog.at_level(logging.WARNING):
            player._report_pacing()
        assert "3 packet(s) were sent late to 10.0.0.9:5555" in caplog.text
    finally:
        local.close()
        remote.close()


def test_speed_must_be_positive(udp_capture) -> None:
    path, _ = udp_capture
    source = replay_source.load(path)
    with pytest.raises(ValueError, match="speed"):
        ReplayServer(source, host="127.0.0.1", port=0, speed=0)


def test_an_empty_password_is_rejected_rather_than_disabling_auth(udp_capture) -> None:
    path, _ = udp_capture
    source = replay_source.load(path)
    credentials = {"username": "admin", "pass" + "word": ""}
    with pytest.raises(ValueError, match="password must not be empty"):
        ReplayServer(source, host="127.0.0.1", port=0, **credentials)


def test_a_username_without_a_password_is_rejected(udp_capture) -> None:
    path, _ = udp_capture
    source = replay_source.load(path)
    with pytest.raises(ValueError, match="together"):
        ReplayServer(source, host="127.0.0.1", port=0, username="admin")


def test_a_path_is_matched_by_segment_not_substring(udp_capture, server_factory) -> None:
    """`/replay` must not answer for `/replayXYZ`."""
    path, _ = udp_capture
    server = server_factory(path)
    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        assert client.request("DESCRIBE", f"rtsp://127.0.0.1:{server.port}/replayXYZ").status == 404
        assert (
            client.request("DESCRIBE", f"rtsp://127.0.0.1:{server.port}/replay?tcp=1").status == 200
        )
    finally:
        client.close()


def test_speed_shortens_playback(tmp_path: Path, server_factory) -> None:
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(10), interval=0.05)
    server = server_factory(path, loop=False, speed=10.0)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")
        start = time.perf_counter()
        received = client.interleaved(len(packets))
        elapsed = time.perf_counter() - start
    finally:
        client.close()

    assert received == packets
    # 0.45s of capture at 10x, plus the start delay: nowhere near real time.
    assert elapsed < 0.3


def test_two_readers_get_independent_streams(tmp_path: Path, server_factory) -> None:
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(5), interval=0.01)
    server = server_factory(path, loop=False)

    first = RtspClient("127.0.0.1", server.port, "replay")
    second = RtspClient("127.0.0.1", server.port, "replay")
    try:
        for client in (first, second):
            client.request(
                "SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1"
            )
            client.request("PLAY")
        assert first.interleaved(len(packets)) == packets
        assert second.interleaved(len(packets)) == packets
    finally:
        first.close()
        second.close()


def test_rtsp_url_reports_a_reachable_address(udp_capture, server_factory) -> None:
    path, _ = udp_capture
    server = server_factory(path)
    assert server.rtsp_url() == f"rtsp://127.0.0.1:{server.port}/replay"


def test_interleaved_frames_are_well_formed(tmp_path: Path, server_factory) -> None:
    """The `$` framing must declare the exact payload length (RFC 2326 §10.12)."""
    path = tmp_path / "tcp.pcap"
    packets = write_interleaved_capture(path, packets=rtp_series(2), interval=0.005)
    server = server_factory(path, loop=False)

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=4-5")
        client.request("PLAY")
        client.socket.settimeout(3)
        raw = b""
        while len(raw) < 4 + len(packets[0]):
            raw += client.socket.recv(65536)
    finally:
        client.close()

    assert raw[0:1] == b"$"
    assert raw[1] == 4
    assert struct.unpack("!H", raw[2:4])[0] == len(packets[0])
    assert raw[4 : 4 + len(packets[0])] == packets[0]


def test_shutdown_drops_a_playing_reader(tmp_path: Path) -> None:
    """`shutdown()` must mean it, even for a reader that never speaks again.

    The handler thread parks in `recv` and the player thread streams from a
    separate deadline loop, so neither notices a closed listening socket. Both
    are daemons, which makes a leak invisible until something long-lived holds
    a server.
    """
    path = tmp_path / "tcp.pcap"
    write_interleaved_capture(path, packets=rtp_series(200), interval=0.005)
    server = ReplayServer(replay_source.load(path), host="127.0.0.1", port=0, path="replay")
    server.start()

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")
        time.sleep(0.1)

        server.shutdown()

        client.socket.settimeout(2)
        drained = 0
        while True:
            chunk = client.socket.recv(65536)
            if not chunk:
                break  # the server hung up, which is the point
            drained += len(chunk)
            assert drained < 1_000_000, "server is still streaming after shutdown"
    finally:
        client.close()

    assert not [thread for thread in threading.enumerate() if thread.name.startswith("vcam-replay")]


def test_shutdown_waits_even_when_another_thread_closed_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second caller into close() must wait for the player too.

    ``close()`` is reached from the handler's cleanup and from ``shutdown()``
    at the same time. ``stop_player`` used to claim the player before joining
    it, so whichever caller arrived second saw ``None`` and returned at once --
    and ``shutdown()`` handed back control while RTP was still in flight. On a
    laptop the loser of that race finished quickly enough to hide it; macOS CI
    failed on it.

    The player's unwind is slowed here so the race is decided by the code under
    test rather than by scheduling luck.
    """
    from vcam import rtsp_replay

    original_run = rtsp_replay._Player.run

    def slow_run(self: rtsp_replay._Player) -> None:
        try:
            original_run(self)
        finally:
            time.sleep(0.4)

    monkeypatch.setattr(rtsp_replay._Player, "run", slow_run)

    path = tmp_path / "race.pcap"
    write_interleaved_capture(path, packets=rtp_series(400), interval=0.005)
    server = ReplayServer(replay_source.load(path), host="127.0.0.1", port=0, path="replay")
    server.start()

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")
        time.sleep(0.15)

        connection = next(iter(server._connections))
        rival = threading.Thread(target=connection.close, name="rival-closer")
        rival.start()
        time.sleep(0.02)  # let the rival claim the player first

        server.shutdown()
        # Sampled before joining the rival on purpose: the rival waits for the
        # player itself, so joining it first would mask exactly the bug this
        # covers. The contract is that *shutdown* leaves nothing streaming.
        alive_at_shutdown = [
            thread.name for thread in threading.enumerate() if thread.name.startswith("vcam-replay")
        ]
        rival.join(timeout=5)
    finally:
        client.close()

    assert not alive_at_shutdown, f"shutdown() returned with {alive_at_shutdown} still running"


def test_shutdown_waits_for_a_reader_whose_handler_is_already_cleaning_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection stays visible to shutdown() until its player is dead.

    When the reader hangs up, the handler's ``finally`` tears the connection
    down. It used to unregister first, which left a window where the connection
    was gone from the server's set while its player thread was still unwinding:
    ``shutdown()`` snapshotted an empty set, did nothing, and returned with RTP
    still going out. Closing first keeps the connection reachable for exactly
    as long as it can still stream.
    """
    from vcam import rtsp_replay

    original_run = rtsp_replay._Player.run

    def slow_run(self: rtsp_replay._Player) -> None:
        try:
            original_run(self)
        finally:
            time.sleep(0.4)

    monkeypatch.setattr(rtsp_replay._Player, "run", slow_run)

    path = tmp_path / "cleanup.pcap"
    write_interleaved_capture(path, packets=rtp_series(400), interval=0.005)
    server = ReplayServer(replay_source.load(path), host="127.0.0.1", port=0, path="replay")
    server.start()

    client = RtspClient("127.0.0.1", server.port, "replay")
    client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
    client.request("PLAY")
    time.sleep(0.15)

    # Drop the reader, then let the handler get into its cleanup before asking
    # the server to stop. That is the window the old ordering opened.
    client.close()
    time.sleep(0.05)

    server.shutdown()
    alive_at_shutdown = [
        thread.name for thread in threading.enumerate() if thread.name.startswith("vcam-replay")
    ]

    assert not alive_at_shutdown, f"shutdown() returned with {alive_at_shutdown} still running"


def test_shutdown_is_safe_without_any_readers(udp_capture) -> None:
    path, _ = udp_capture
    server = ReplayServer(replay_source.load(path), host="127.0.0.1", port=0, path="replay")
    server.start()
    server.shutdown()
    server.shutdown()  # idempotent: the CLI calls it from a `finally` after Ctrl-C


def test_reader_is_disconnected_when_a_non_looping_capture_ends(tmp_path: Path) -> None:
    """A finished capture must hang up rather than leave the reader waiting.

    Without this, `--no-loop` strands every client: the player returns, the
    connection stays open with no further data, and a reader parked in recv
    waits forever. Observed with real ffmpeg, which sat on an exhausted
    12-second capture for minutes instead of finalising its output file.
    """
    path = tmp_path / "short.pcap"
    write_interleaved_capture(path, packets=rtp_series(6), interval=0.005)
    server = ReplayServer(
        replay_source.load(path), host="127.0.0.1", port=0, path="replay", loop=False
    )
    server.start()

    client = RtspClient("127.0.0.1", server.port, "replay")
    try:
        client.request("SETUP", f"{client.url}/trackID=0", Transport="RTP/AVP/TCP;interleaved=0-1")
        client.request("PLAY")

        client.socket.settimeout(5)
        while True:
            chunk = client.socket.recv(65536)
            if not chunk:
                break  # EOF: the server hung up, which is the point
    finally:
        client.close()
        server.shutdown()
