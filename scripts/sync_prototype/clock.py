"""Pure rational timing and marker-observer checks for the bounded prototype."""

from __future__ import annotations

from vcam.scene_clock import SceneClock as SceneClock


def alignment_report(
    samples: list[dict], frame_ms: float, expected_views: tuple[int, ...] = (0, 1, 2)
) -> dict:
    by_frame: dict[int, dict[int, list[int]]] = {}
    for sample in samples:
        frame = sample["global_frame"]
        view = sample["view"]
        by_frame.setdefault(frame, {}).setdefault(view, []).append(sample["arrival_ns"])
    spreads = []
    duplicates = 0
    for views in by_frame.values():
        duplicates += sum(len(times) - 1 for times in views.values())
        if set(views) == set(expected_views):
            times = [min(times) for times in views.values()]
            spreads.append((max(times) - min(times)) / 1_000_000)
    ordered = sorted(spreads)
    return {
        "matched_frames": len(spreads),
        "duplicates": duplicates,
        "receive_spread_ms": {
            "p50": ordered[len(ordered) // 2] if ordered else None,
            "p99": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] if ordered else None,
            "max": max(ordered) if ordered else None,
        },
        "violations": sum(value > frame_ms for value in spreads),
        "threshold_ms": frame_ms,
        "measurement": "decoded same-scene-frame receipt spread on one observer host",
        "scope": "controlled readers only; not arbitrary display or RTCP accuracy",
    }
