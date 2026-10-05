"""Bounded, owned loopback TCP proxy for fault injection and wire observations."""

from __future__ import annotations

import selectors
import socket
import struct
import threading
import time

from vcam import rtp
from vcam.errors import RtspFramingError
from vcam.rtsp_messages import InterleavedFrame, RtspStreamParser

BUFFER_LIMIT = 65536


class Proxy:
    def __init__(self, target_port: int, *, inspect: bool = False) -> None:
        self.target_port = target_port
        self.inspect = inspect
        self.paused = threading.Event()
        self.stop = threading.Event()
        self.expected_disconnect = threading.Event()
        self.records: list[dict] = []
        self.errors: list[str] = []
        self.expected_errors: list[str] = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.run, name="vcam-sync-proxy")
        self.thread.start()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            try:
                with (
                    client,
                    socket.create_connection(("127.0.0.1", self.target_port), timeout=2) as server,
                ):
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    self.bridge(client, server)
            except (OSError, ValueError, RtspFramingError) as exc:
                if not self.stop.is_set():
                    destination = (
                        self.expected_errors
                        if isinstance(exc, OSError) and self.expected_disconnect.is_set()
                        else self.errors
                    )
                    destination.append(str(exc))

    def bridge(self, client: socket.socket, server: socket.socket) -> None:
        parser = RtspStreamParser()
        buffers = {client: bytearray(), server: bytearray()}
        peer = {client: server, server: client}
        for sock in peer:
            sock.setblocking(False)
        with selectors.DefaultSelector() as selector:
            for sock in peer:
                selector.register(sock, selectors.EVENT_READ)
            while not self.stop.is_set():
                for sock in peer:
                    writing = buffers[sock] and not (sock is server and self.paused.is_set())
                    reading = len(buffers[peer[sock]]) < BUFFER_LIMIT and not (
                        sock is client and self.paused.is_set()
                    )
                    mask = (selectors.EVENT_READ if reading else 0) | (
                        selectors.EVENT_WRITE if writing else 0
                    )
                    if mask:
                        if sock in selector.get_map():
                            selector.modify(sock, mask)
                        else:
                            selector.register(sock, mask)
                    elif sock in selector.get_map():
                        selector.unregister(sock)
                for key, mask in selector.select(0.05):
                    ready_socket = key.fileobj
                    if not isinstance(ready_socket, socket.socket):
                        raise ValueError("proxy selector returned a non-socket")
                    sock = ready_socket
                    if mask & selectors.EVENT_READ:
                        try:
                            data = sock.recv(min(8192, BUFFER_LIMIT - len(buffers[peer[sock]])))
                        except BlockingIOError:
                            continue
                        if not data:
                            return
                        if self.inspect and sock is server:
                            for item in parser.feed(data):
                                if isinstance(item, InterleavedFrame):
                                    self.record(item.payload)
                        buffers[peer[sock]].extend(data)
                    if mask & selectors.EVENT_WRITE:
                        try:
                            sent = sock.send(buffers[sock])
                        except BlockingIOError:
                            continue
                        del buffers[sock][:sent]

    def record(self, data: bytes) -> None:
        now = time.monotonic_ns()
        header = rtp.parse(data)
        if header and header.marker:
            self.records.append(
                {
                    "kind": "rtp",
                    "arrival_ns": now,
                    "ssrc": header.ssrc,
                    "sequence": header.sequence,
                    "timestamp": header.timestamp,
                }
            )
        elif len(data) >= 28 and data[1] == 200:
            ssrc, seconds, fraction, timestamp = struct.unpack_from("!IIII", data, 4)
            self.records.append(
                {
                    "kind": "sr",
                    "arrival_ns": now,
                    "ssrc": ssrc,
                    "ntp_unix": seconds - 2208988800 + fraction / (1 << 32),
                    "timestamp": timestamp,
                }
            )

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=3)
        self.listener.close()
        if self.thread.is_alive():
            raise RuntimeError("owned proxy thread failed to stop")
