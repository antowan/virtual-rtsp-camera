"""Atomic bounded local telemetry, without locks shared with suspended children."""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
from dataclasses import dataclass, field

MAX_EVENT_BYTES = 8000


@dataclass
class EventSink:
    address: tuple[str, int]
    token: str
    sequence: int = 0
    _socket: socket.socket | None = field(default=None, repr=False)

    def put(self, event: dict) -> None:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(0.1)
        self.sequence += 1
        data = json.dumps(
            {
                "token": self.token,
                "pid": os.getpid(),
                "sequence": self.sequence,
                "event": event,
            }
        ).encode()
        if len(data) > MAX_EVENT_BYTES:
            raise ValueError("prototype telemetry event exceeds bounded datagram size")
        self._socket.sendto(data, self.address)


class EventReceiver:
    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.socket.bind(("127.0.0.1", 0))
        self.token = secrets.token_hex(32)
        self.sequences: dict[int, int] = {}
        self.gaps: list[dict] = []

    def sink(self) -> EventSink:
        return EventSink(self.socket.getsockname(), self.token)

    def get(self, timeout: float) -> dict:
        self.socket.settimeout(timeout)
        try:
            data, _ = self.socket.recvfrom(MAX_EVENT_BYTES + 1)
        except TimeoutError as exc:
            raise queue.Empty from exc
        if len(data) > MAX_EVENT_BYTES:
            raise ValueError("oversized telemetry datagram")
        message = json.loads(data)
        if not secrets.compare_digest(message["token"], self.token):
            raise ValueError("unexpected sender on private telemetry listener")
        pid, sequence = message["pid"], message["sequence"]
        previous = self.sequences.get(pid, 0)
        if sequence != previous + 1:
            self.gaps.append({"pid": pid, "previous": previous, "sequence": sequence})
        self.sequences[pid] = sequence
        return message["event"]

    def close(self) -> None:
        self.socket.close()
