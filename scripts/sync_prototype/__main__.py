"""Run with `uv run --extra prototype python -m scripts.sync_prototype`."""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import platform
import queue
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from vcam.binaries import resolve_binary
from vcam.mediamtx import ServerInstance, render_server_config
from vcam.models import CameraSpec, ServerSpec

from .clock import SceneClock, alignment_report, nearest_global_frame
from .events import EventReceiver
from .media import FRAMES, RATE, dependencies, generate_fixture, observer, stop_process, worker
from .proxy import Proxy


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_config(port: int) -> dict:
    config = render_server_config(
        ServerInstance(
            rtsp_port=port,
            api_port=free_port(),
            cameras=[CameraSpec(name=f"view{i}", source=Path("unused")) for i in range(3)],
        ),
        ServerSpec(host="127.0.0.1"),
    )
    config.update({"api": False, "rtspTransports": ["tcp"], "logLevel": "info"})
    return config


def listener_reported(log: str, port: int) -> bool:
    return any(
        f"[RTSP] {message} 127.0.0.1:{port} (" in log
        for message in ("listener opened on", "started with listeners on")
    )


def run_experiment(
    duration: float, b_frames: int, scenario: str, binary: Path, directory: Path
) -> dict:
    ctx = mp.get_context("spawn")
    epoch = ctx.Value("q", 0)
    start = ctx.Event()
    events = EventReceiver()
    workers: dict[int, tuple] = {}
    readers: dict[str, tuple] = {}
    proxies = []
    proxy_views: dict[Proxy, int] = {}
    report: dict = {
        "schema_version": 1,
        "scenario": scenario,
        "b_frames": b_frames,
        "duration_seconds": duration,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "fixtures": [],
        "events": [],
        "samples": [],
        "resources": [],
        "wire": {},
        "errors": [],
    }
    av, _ = dependencies()
    report["backend"] = {"pyav": av.__version__, "libraries": av.library_versions}
    report["mediamtx"] = subprocess.check_output([str(binary), "--version"], text=True).strip()
    for view in range(3):
        path = directory / f"view{view}.mp4"
        report["fixtures"].append(generate_fixture(path, view, b_frames))
    port = free_port()
    config_path = directory / "server.yml"
    config_path.write_text(yaml.safe_dump(server_config(port)))
    server_log = (directory / "server.log").open("w")
    server = subprocess.Popen(
        [str(binary), str(config_path)],
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    generation = [0, 0, 0]
    last_sent: dict[int, int] = {}
    joined_at: dict[int, int] = {}
    expected_disconnects: set[str] = set()
    fault_started = False
    fault_finished = False
    late_started = False
    reconnect_started = False
    reader_retries: dict[str, int] = {}
    reader_view: dict[str, int] = {}
    reader_sessions: dict[str, int] = {}
    observer_origins: dict[tuple[str, int], tuple[float, int]] = {}
    side_recovered = False

    def launch_worker(view: int) -> None:
        stop = ctx.Event()
        process = ctx.Process(
            target=worker,
            args=(
                str(directory / f"view{view}.mp4"),
                f"rtsp://127.0.0.1:{publish[view].port}/view{view}",
                view,
                generation[view],
                epoch,
                start,
                stop,
                events.sink(),
            ),
        )
        process.start()
        workers[view] = (process, stop)
        joined_at[view] = time.monotonic_ns()
        last_sent.pop(view, None)

    def launch_reader(view: int, name: str) -> None:
        reader_view[name] = view
        reader_sessions[name] = reader_sessions.get(name, -1) + 1
        stop = ctx.Event()
        proxy = Proxy(port, inspect=True)
        proxies.append(proxy)
        proxy_views[proxy] = view
        process = ctx.Process(
            target=observer,
            args=(
                f"rtsp://127.0.0.1:{proxy.port}/view{view}",
                view,
                name,
                reader_sessions[name],
                start,
                stop,
                events.sink(),
            ),
        )
        process.start()
        readers[name] = (process, stop, proxy)

    def restart(view: int, reason: str) -> None:
        process, stop = workers[view]
        stop_process(process, stop, timeout=0.5)
        generation[view] += 1
        report["events"].append(
            {
                "kind": "restart",
                "view": view,
                "reason": reason,
                "generation": generation[view],
                "at_ns": time.monotonic_ns(),
            }
        )
        expected_disconnects.add(f"main{view}")
        launch_worker(view)

    try:
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                raise RuntimeError("owned MediaMTX failed to start; see server log")
            if not listener_reported((directory / "server.log").read_text(), port):
                if time.monotonic() > deadline:
                    raise RuntimeError("owned MediaMTX did not report its listener")
                time.sleep(0.05)
                continue
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("owned MediaMTX readiness timeout") from None
                time.sleep(0.05)
        publish = [Proxy(port) for _ in range(3)]
        proxies.extend(publish)
        proxy_views.update({proxy: view for view, proxy in enumerate(publish)})
        for view in range(3):
            launch_worker(view)
        ready: set[int] = set()
        event: dict | None
        deadline = time.monotonic() + 15
        while ready != {0, 1, 2}:
            if time.monotonic() > deadline:
                raise RuntimeError("initial publisher barrier timed out")
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue
            report["events"].append(event)
            if event["kind"] == "error":
                raise RuntimeError(event["detail"])
            if event["kind"] == "ready":
                ready.add(event["view"])
        epoch.value = time.monotonic_ns() + 1_000_000_000
        report["epoch_ns"] = epoch.value
        start.set()
        for view in range(3):
            launch_reader(view, f"main{view}")
        clock = SceneClock(epoch.value)
        while (time.monotonic_ns() - epoch.value) / 1_000_000_000 < duration:
            now_ns = time.monotonic_ns()
            elapsed = (now_ns - epoch.value) / 1_000_000_000
            if elapsed > 5 and not late_started:
                launch_reader(0, "late")
                late_started = True
            if elapsed > 9 and not reconnect_started:
                process, stop, proxy = readers["late"]
                proxy.expected_disconnect.set()
                stop_process(process, stop)
                launch_reader(0, "reconnect")
                reconnect_started = True
            if scenario != "baseline" and elapsed >= 12 and not fault_started:
                report["events"].append({"kind": "fault_begin", "at_ns": now_ns})
                expected_disconnects.add("main1")
                if scenario == "exit":
                    restart(1, "injected_exit")
                elif scenario == "stall":
                    if not hasattr(signal, "SIGSTOP"):
                        raise RuntimeError("stall experiment requires POSIX SIGSTOP")
                    process, _ = workers[1]
                    os.kill(process.pid, signal.SIGSTOP)
                else:
                    publish[1].paused.set()
                fault_started = True
            if (
                fault_started
                and not fault_finished
                and elapsed >= (22 if scenario == "backpressure" else 14)
            ):
                publish[1].paused.clear()
                report["events"].append({"kind": "fault_end", "at_ns": now_ns})
                fault_finished = True
            try:
                event = events.get(timeout=0.02)
            except queue.Empty:
                event = None
            if event:
                kind = event["kind"]
                if kind == "observed":
                    session = event["observer_session"]
                    key = (event["name"], session)
                    if key not in observer_origins:
                        expected = clock.global_frame(event["arrival_ns"])
                        origin = nearest_global_frame(event["marker"], expected, FRAMES)
                        observer_origins[key] = (event["pts_seconds"], origin)
                    pts_origin, scene_origin = observer_origins[key]
                    delta = (event["pts_seconds"] - pts_origin) * RATE
                    event["global_frame"] = scene_origin + round(delta)
                    if (
                        abs(delta - round(delta)) > 0.001
                        or event["global_frame"] % FRAMES != event["marker"]
                    ):
                        report["errors"].append(
                            {
                                "kind": "marker_transport_mismatch",
                                "name": event["name"],
                            }
                        )
                    if (
                        event["name"] == "main1"
                        and session > 0
                        and not side_recovered
                        and (scenario != "backpressure" or fault_finished)
                    ):
                        side_recovered = True
                        report["events"].append(
                            {
                                "kind": "observed_recovery",
                                "view": 1,
                                "at_ns": event["arrival_ns"],
                                "global_frame": event["global_frame"],
                            }
                        )
                    event["eligible"] = not (
                        event["view"] == 1 and fault_started and not side_recovered
                    )
                    report["samples"].append(event)
                elif kind == "resource":
                    report["resources"].append(event)
                else:
                    report["events"].append(event)
                if kind == "sent":
                    last_sent[event["view"]] = event["sent_ns"]
                elif kind == "joined":
                    joined_at[event["view"]] = now_ns
                elif kind == "observer_error":
                    name = event["name"]
                    if name not in expected_disconnects:
                        report["errors"].append(event)
                elif kind == "error" and (event["view"] != 1 or scenario == "baseline"):
                    report["errors"].append(event)
            for view, (process, _) in list(workers.items()):
                ready_age = now_ns - joined_at[view]
                progress_age = now_ns - last_sent.get(view, joined_at[view])
                if not process.is_alive() or (
                    ready_age > 3_000_000_000 and progress_age > 1_000_000_000
                ):
                    if generation[view] >= 8:
                        raise RuntimeError(f"view{view} exhausted bounded recovery attempts")
                    restart(view, "exit" if not process.is_alive() else "progress_watchdog")
            for name, (process, stop, _) in list(readers.items()):
                if name == "late" and reconnect_started:
                    continue
                if process.is_alive() or name not in expected_disconnects:
                    continue
                stop_process(process, stop)
                if reader_retries.get(name, 0) >= 8:
                    raise RuntimeError(f"reader {name} exhausted reconnect attempts")
                reader_retries[name] = reader_retries.get(name, 0) + 1
                launch_reader(reader_view[name], name)
        report["alignment"] = alignment_report(
            [
                sample
                for sample in report["samples"]
                if sample["name"].startswith("main") and sample["eligible"]
            ],
            1000 / RATE,
        )
        report["telemetry_gaps"] = events.gaps
        if events.gaps:
            report["errors"].append({"kind": "telemetry_loss"})
        validate_report(report)
    except Exception as exc:
        report["errors"].append({"kind": "experiment_error", "detail": str(exc)})
        report["verdict"] = "failed"
    finally:
        for process, stop, proxy in readers.values():
            proxy.expected_disconnect.set()
            stop_process(process, stop)
        for process, stop in workers.values():
            stop_process(process, stop)
        for index, proxy in enumerate(proxies):
            proxy.close()
            if proxy.records:
                report["wire"][str(index)] = proxy.records
            if proxy.expected_errors:
                report["events"].append(
                    {
                        "kind": "expected_proxy_disconnect",
                        "proxy": index,
                        "errors": proxy.expected_errors,
                    }
                )
            if proxy.errors:
                report["events"].append(
                    {"kind": "proxy_errors", "proxy": index, "errors": proxy.errors}
                )
                if scenario == "baseline" or proxy_views[proxy] != 1:
                    report["errors"].append({"kind": "unexpected_proxy_error", "proxy": index})
                    report["verdict"] = "failed"
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)
        server_log.close()
        report["server_log"] = (directory / "server.log").read_text()
        events.close()
    return report


def validate_report(report: dict) -> None:
    errors = report["errors"]
    alignment = report["alignment"]
    if alignment["matched_frames"] < int(report["duration_seconds"] * RATE * 0.5):
        errors.append({"kind": "insufficient_overlap"})
    if alignment["violations"] or alignment["duplicates"]:
        errors.append({"kind": "alignment_failed"})
    main = [
        sample
        for sample in report["samples"]
        if sample["name"].startswith("main") and sample["eligible"]
    ]
    peer_alignment = alignment_report(
        [sample for sample in main if sample["view"] in (0, 2)],
        1000 / RATE,
        expected_views=(0, 2),
    )
    report["healthy_peer_alignment"] = peer_alignment
    if (
        peer_alignment["violations"]
        or peer_alignment["duplicates"]
        or peer_alignment["matched_frames"] < int(report["duration_seconds"] * RATE * 0.5)
    ):
        errors.append({"kind": "healthy_peer_alignment_failed"})
    gaps = []
    for view in range(3):
        samples = [sample for sample in main if sample["view"] == view]
        previous = None
        for sample in samples:
            if (
                previous is not None
                and previous["observer_session"] == sample["observer_session"]
                and sample["global_frame"] != previous["global_frame"] + 1
            ):
                gaps.append(
                    {
                        "view": view,
                        "from": previous["global_frame"],
                        "to": sample["global_frame"],
                    }
                )
            previous = sample
    report["healthy_frame_gaps"] = gaps
    if gaps:
        errors.append({"kind": "healthy_frame_gaps"})
    maximum_age = max(
        (
            (sample["arrival_ns"] - report["epoch_ns"]) / 1_000_000_000
            - (sample["global_frame"] + 3) / RATE
            for sample in main
        ),
        default=0,
    )
    report["max_decoded_age_seconds"] = maximum_age
    # B-frame decoder preroll is a known latency, not scene phase; reject stale backlog.
    if maximum_age > 0.5:
        errors.append({"kind": "stale_content"})
    healthy_views = (0, 1, 2) if report["scenario"] == "baseline" else (0, 2)
    for view in healthy_views:
        arrivals = [sample["arrival_ns"] for sample in main if sample["view"] == view]
        if not arrivals or (
            report["epoch_ns"] + report["duration_seconds"] * 1_000_000_000 - max(arrivals)
            > 500_000_000
        ):
            errors.append({"kind": "healthy_peer_stopped_decoding", "view": view})
        unhealthy = [
            event
            for event in report["events"]
            if event.get("view") == view and event["kind"] in {"restart", "error", "skipped"}
        ]
        if unhealthy:
            errors.append({"kind": "healthy_peer_interrupted", "view": view})
    for name in ("late", "reconnect"):
        samples = [sample for sample in report["samples"] if sample["name"] == name]
        if not samples or samples[0]["global_frame"] < (4 if name == "late" else 8) * RATE:
            errors.append({"kind": "late_join_failed", "name": name})
    sent = [event for event in report["events"] if event["kind"] == "sent"]
    report["max_write_lateness_ms"] = max((event["lateness_ms"] for event in sent), default=None)
    if report["scenario"] != "baseline":
        recovered = [
            event
            for event in sent
            if event["view"] == 1 and event["generation"] > 0 and event["global_frame"] >= 23 * RATE
        ]
        if not recovered:
            errors.append({"kind": "recovery_unproven"})
        recoveries = [event for event in report["events"] if event["kind"] == "observed_recovery"]
        beginnings = [event for event in report["events"] if event["kind"] == "fault_begin"]
        report["observed_outage_seconds"] = (
            (recoveries[0]["at_ns"] - beginnings[0]["at_ns"]) / 1_000_000_000
            if recoveries and beginnings
            else None
        )
        if not recoveries:
            errors.append({"kind": "decoded_recovery_unproven"})
        if report["scenario"] in {"stall", "backpressure"} and not any(
            event["kind"] == "restart" and event.get("reason") == "progress_watchdog"
            for event in report["events"]
        ):
            errors.append({"kind": "publisher_stall_not_observed"})
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
        "Copy mode on bounded generated fixtures only; not a production backend.",
        "Decoded receipt spread includes OS, proxy and decoder latency; no display guarantee.",
        "Cyclic markers cannot independently distinguish a whole-scene-period lag.",
        "Wire RTP/SR records are diagnostics, not an RTCP wall-clock accuracy assertion.",
        "No automated SR-to-decoded-frame mapping or hardware/load certification yet.",
        "OS scheduling uncertainty is not certified below 5 ms; pass is not a maximum SLA.",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=44)
    parser.add_argument("--b-frames", type=int, choices=(0, 2), default=0)
    parser.add_argument(
        "--scenario", choices=("baseline", "exit", "stall", "backpressure"), default="baseline"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mediamtx-binary", type=Path)
    args = parser.parse_args()
    if not 28 <= args.duration <= 120:
        parser.error("--duration must be between 28 and 120 seconds")
    if args.report.exists():
        parser.error("--report already exists; choose a new output path")
    binary = resolve_binary(args.mediamtx_binary, allow_download=False)
    with tempfile.TemporaryDirectory(prefix="vcam-sync-prototype-") as directory:
        report = run_experiment(
            args.duration, args.b_frames, args.scenario, binary, Path(directory)
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "alignment": report.get("alignment"),
                "errors": report["errors"],
                "report": str(args.report),
            },
            indent=2,
        )
    )
    return 0 if report["verdict"] == "passed_controlled_profile" else 1


if __name__ == "__main__":
    raise SystemExit(main())
