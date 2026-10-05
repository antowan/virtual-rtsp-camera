"""Explicit recovery window, scoped to the current publication generation."""

from __future__ import annotations

from dataclasses import dataclass

from .media import RATE


@dataclass
class RecoveryWindow:
    generation: int = 0
    active: bool = False
    first_ns: int | None = None
    last_frame: int | None = None
    session: int | None = None
    frames: int = 0

    def begin(self, generation: int) -> None:
        self.generation = generation
        self.active = True
        self.first_ns = None
        self.last_frame = None
        self.session = None
        self.frames = 0

    def observe(self, sample: dict) -> dict | None:
        if not self.active or sample["publisher_generation"] != self.generation:
            return None
        frame, session = sample["global_frame"], sample["observer_session"]
        if self.session != session or self.last_frame != frame - 1:
            self.first_ns = sample["arrival_ns"]
            self.frames = 0
        self.session, self.last_frame = session, frame
        self.frames += 1
        if self.frames < RATE:
            return None
        self.active = False
        return {
            "kind": "observed_recovery",
            "view": sample["view"],
            "generation": self.generation,
            "observer_session": session,
            "at_ns": self.first_ns,
            "confirmed_at_ns": sample["arrival_ns"],
            "global_frame": frame,
            "consecutive_frames": self.frames,
        }

    def expects_reader_failure(
        self, name: str, session: int, disconnects: set[tuple[str, int]]
    ) -> bool:
        return (name, session) in disconnects or (name == "main1" and self.active)
