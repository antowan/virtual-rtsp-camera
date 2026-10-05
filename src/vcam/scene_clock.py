"""Exact monotonic scene scheduling, independent of transport timestamp origins."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class SceneClock:
    epoch_ns: int
    rate: Fraction = Fraction(30)
    frames: int = 120
    lead_frames: int = 3

    def deadline_ns(self, decode_frame: int) -> int:
        return self.epoch_ns + int((decode_frame + self.lead_frames) * 1_000_000_000 / self.rate)

    def global_frame(self, now_ns: int) -> int:
        return math.floor(
            Fraction(now_ns - self.epoch_ns, 1_000_000_000) * self.rate - self.lead_frames
        )

    def future_access_point(self, now_ns: int, points: tuple[int, ...], delay: int) -> int:
        if not points:
            raise ValueError("no independently decodable access points")
        minimum = max(0, self.global_frame(now_ns) + delay)
        loop = minimum // self.frames
        for point in points:
            candidate = loop * self.frames + point
            if candidate >= minimum:
                return candidate
        return (loop + 1) * self.frames + points[0]
