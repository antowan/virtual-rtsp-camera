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
    producer: str
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
                "producer": self.producer,
                "sequence": self.sequence,
                "event": event,
            }
        ).encode()
        if len(data) > MAX_EVENT_BYTES:
            raise ValueError("prototype telemetry event exceeds bounded datagram size")
        self._socket.sendto(data, self.address)

    def finish(self) -> None:
        self.put({"kind": "telemetry_end", "producer": self.producer})
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class EventReceiver:
    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.socket.bind(("127.0.0.1", 0))
        self.token = secrets.token_hex(32)
        self.sequences: dict[str, int] = {}
        self.gaps: list[dict] = []
        self.producers: dict[str, dict] = {}

    def sink(self) -> EventSink:
        producer = secrets.token_hex(16)
        self.producers[producer] = {"complete": False, "expected_incomplete": None}
        return EventSink(self.socket.getsockname(), self.token, producer)

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
        producer, sequence = message["producer"], message["sequence"]
        if producer not in self.producers:
            raise ValueError("unregistered telemetry producer")
        if self.producers[producer]["complete"]:
            raise ValueError("telemetry arrived after the producer's terminal record")
        previous = self.sequences.get(producer, 0)
        if sequence != previous + 1:
            self.gaps.append({"producer": producer, "previous": previous, "sequence": sequence})
        self.sequences[producer] = sequence
        if message["event"]["kind"] == "telemetry_end":
            self.producers[producer]["complete"] = True
        return message["event"]

    def allow_incomplete(self, producer: str, reason: str) -> None:
        self.producers[producer]["expected_incomplete"] = reason

    def evidence(self) -> dict:
        return {
            "producers": self.producers,
            "gaps": self.gaps,
            "incomplete": [
                producer
                for producer, state in self.producers.items()
                if not state["complete"] and state["expected_incomplete"] is None
            ],
        }

    def close(self) -> None:
        self.socket.close()
