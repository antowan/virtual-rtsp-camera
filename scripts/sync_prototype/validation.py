"""Pure acceptance checks: transport/content identity, reader health and recovery."""

from __future__ import annotations

from .clock import alignment_report
from .media import FRAMES, RATE

MAX_AGE_SECONDS = 0.5
MIN_JOIN_FRAMES = RATE


def validate_report(report: dict) -> None:
    errors = report["errors"]
    epoch = report["epoch_ns"]
    end_ns = epoch + report["duration_seconds"] * 1_000_000_000
    samples = report["samples"]
    main = [
        sample for sample in samples if sample["name"].startswith("main") and sample["eligible"]
    ]
    alignment = alignment_report(main, 1000 / RATE)
    report["alignment"] = alignment
    if alignment["matched_frames"] < int(report["duration_seconds"] * RATE * 0.5):
        errors.append({"kind": "insufficient_overlap"})
    if alignment["violations"] or alignment["duplicates"]:
        errors.append({"kind": "alignment_failed"})
    peers = alignment_report(
        [sample for sample in main if sample["view"] in (0, 2)],
        1000 / RATE,
        expected_views=(0, 2),
    )
    report["healthy_peer_alignment"] = peers
    if (
        peers["violations"]
        or peers["duplicates"]
        or peers["matched_frames"] < int(report["duration_seconds"] * RATE * 0.5)
    ):
        errors.append({"kind": "healthy_peer_alignment_failed"})
    gaps = []
    ages = []
    for name in ("main0", "main1", "main2", "late", "reconnect"):
        reader = [sample for sample in samples if sample["name"] == name and sample["eligible"]]
        if len(reader) < MIN_JOIN_FRAMES:
            errors.append({"kind": "insufficient_reader_progress", "name": name})
        previous = None
        for sample in reader:
            age = (sample["arrival_ns"] - epoch) / 1_000_000_000 - (
                sample["global_frame"] + 3
            ) / RATE
            ages.append(age)
            if age > MAX_AGE_SECONDS or age < -1 / RATE:
                errors.append({"kind": "reader_not_current", "name": name})
                break
            if sample["global_frame"] % FRAMES != sample["marker"]:
                errors.append({"kind": "marker_transport_mismatch", "name": name})
            if previous is not None and (
                previous["observer_session"] == sample["observer_session"]
                and previous["publisher_generation"] == sample["publisher_generation"]
            ):
                delta = (sample["pts_seconds"] - previous["pts_seconds"]) * RATE
                if sample["global_frame"] != previous["global_frame"] + 1 or abs(delta - 1) > 0.001:
                    gaps.append(
                        {
                            "name": name,
                            "from": previous["global_frame"],
                            "to": sample["global_frame"],
                        }
                    )
            previous = sample
        reader_end = epoch + 9_000_000_000 if name == "late" else end_ns
        if not reader or reader_end - max(sample["arrival_ns"] for sample in reader) > 500_000_000:
            errors.append({"kind": "reader_stopped_decoding", "name": name})
        if name in ("late", "reconnect"):
            minimum = (4 if name == "late" else 8) * RATE
            if not reader or reader[0]["global_frame"] < minimum:
                errors.append({"kind": "late_join_failed", "name": name})
            reference = {
                sample["global_frame"]: sample["arrival_ns"]
                for sample in main
                if sample["view"] == 0
            }
            spreads = [
                abs(sample["arrival_ns"] - reference[sample["global_frame"]]) / 1_000_000
                for sample in reader
                if sample["global_frame"] in reference
            ]
            report.setdefault("join_alignment", {})[name] = {
                "matched_frames": len(spreads),
                "max_receive_spread_ms": max(spreads, default=None),
            }
            if len(spreads) < MIN_JOIN_FRAMES or any(spread > 1000 / RATE for spread in spreads):
                errors.append({"kind": "join_alignment_failed", "name": name})
    report["healthy_frame_gaps"] = gaps
    if gaps:
        errors.append({"kind": "healthy_frame_gaps"})
    report["max_decoded_age_seconds"] = max(ages, default=None)
    for view in (0, 1, 2) if report["scenario"] == "baseline" else (0, 2):
        if any(
            event.get("view") == view and event["kind"] in {"restart", "error", "skipped"}
            for event in report["events"]
        ):
            errors.append({"kind": "healthy_peer_interrupted", "view": view})
    sent = [event for event in report["events"] if event["kind"] == "sent"]
    report["max_write_lateness_ms"] = max((event["lateness_ms"] for event in sent), default=None)
    if report["scenario"] != "baseline":
        recoveries = [event for event in report["events"] if event["kind"] == "observed_recovery"]
        beginnings = [event for event in report["events"] if event["kind"] == "fault_begin"]
        report["observed_outage_seconds"] = (
            (recoveries[0]["at_ns"] - beginnings[0]["at_ns"]) / 1_000_000_000
            if recoveries and beginnings
            else None
        )
        if not recoveries or any(event.get("consecutive_frames", 0) < RATE for event in recoveries):
            errors.append({"kind": "decoded_recovery_unproven"})
        for recovery in recoveries:
            if any(
                event.get("view") == 1
                and event["kind"] in ("restart", "skipped")
                and event.get("at_ns", 0) > recovery["confirmed_at_ns"]
                for event in report["events"]
            ):
                errors.append({"kind": "post_recovery_publisher_interrupted"})
        if report["scenario"] in {"stall", "backpressure"} and not any(
            event["kind"] == "restart" and event.get("reason") == "progress_watchdog"
            for event in report["events"]
        ):
            errors.append({"kind": "publisher_stall_not_observed"})
        if not any(
            event["view"] == 1 and event["generation"] > 0 and event["global_frame"] >= 23 * RATE
            for event in sent
        ):
            errors.append({"kind": "recovery_unproven"})
    report["healthy_scene_wraps"] = {
        str(view): sum(sample["marker"] == 0 for sample in main if sample["view"] == view)
        for view in (0, 2)
    }
    if report["duration_seconds"] >= 44 and any(
        wraps < 10 for wraps in report["healthy_scene_wraps"].values()
    ):
        errors.append({"kind": "insufficient_scene_wraps"})
    report["verdict"] = "failed" if errors else "passed_controlled_profile"
    report["limitations"] = [
        "Copy mode with added diagnostic SEI on bounded generated fixtures only.",
        "Decoded identity includes publication generation and global loop; no wall-time anchoring.",
        "Decoded receipt spread includes OS, proxy and decoder latency; no display guarantee.",
        "Wire RTP/SR records are diagnostics, not an RTCP wall-clock accuracy assertion.",
        "No automated SR-to-decoded-frame mapping or hardware/load certification yet.",
        "OS scheduling uncertainty is not certified below 5 ms; pass is not a maximum SLA.",
    ]
