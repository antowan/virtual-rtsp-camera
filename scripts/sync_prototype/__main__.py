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
from collections.abc import Callable
from functools import partial
from pathlib import Path

import yaml

from vcam.binaries import resolve_binary
from vcam.mediamtx import ServerInstance, render_server_config
from vcam.models import CameraSpec, ServerSpec

from .events import EventReceiver
from .lifecycle import cleanup_all, run_bounded
from .media import FRAMES, RATE, dependencies, generate_fixture, observer, stop_process, worker
from .proxy import Proxy
from .recovery import RecoveryWindow
from .validation import validate_report


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
    producers: dict[int | None, str] = {}
    report: dict = {
        "schema_version": 2,
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
    report["mediamtx"] = subprocess.check_output(
        [str(binary), "--version"], text=True, timeout=3
    ).strip()
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
    expected_disconnects: set[tuple[str, int]] = set()
    fault_started = False
    fault_finished = False
    late_started = False
    reconnect_started = False
    reader_retries: dict[str, int] = {}
    reader_view: dict[str, int] = {}
    reader_sessions: dict[str, int] = {}
    observer_origins: dict[tuple[str, int], tuple[float, int]] = {}
    recovery = RecoveryWindow()
    reported_reader_exits: set[tuple[str, int]] = set()

    def launch_worker(view: int) -> None:
        stop = ctx.Event()
        sink = events.sink()
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
                sink,
            ),
        )
        process.start()
        producers[process.pid] = sink.producer
        workers[view] = (process, stop)
        joined_at[view] = time.monotonic_ns()
        last_sent.pop(view, None)

    def launch_reader(view: int, name: str) -> None:
        reader_view[name] = view
        reader_sessions[name] = reader_sessions.get(name, -1) + 1
        stop = ctx.Event()
        sink = events.sink()
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
                sink,
            ),
        )
        process.start()
        producers[process.pid] = sink.producer
        readers[name] = (process, stop, proxy)

    def restart(view: int, reason: str) -> None:
        if view == 1 and not (fault_started and recovery.active):
            report["errors"].append({"kind": "unexpected_publisher_failure", "reason": reason})
        process, stop = workers[view]
        if view == 1 and recovery.active:
            events.allow_incomplete(
                producers[process.pid], "injected member replaced during recovery"
            )
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
        name = f"main{view}"
        expected_disconnects.add((name, reader_sessions.get(name, 0)))
        if view == 1:
            recovery.begin(generation[view])
        launch_worker(view)

    def consume(event: dict) -> None:
        kind = event["kind"]
        if kind == "observed":
            if event["arrival_ns"] > epoch.value + duration * 1_000_000_000:
                report["events"].append({"kind": "after_measurement", "event": event})
                return
            session = event["observer_session"]
            key = (event["name"], session)
            if key not in observer_origins:
                observer_origins[key] = (event["pts_seconds"], event["global_frame"])
            pts_origin, scene_origin = observer_origins[key]
            delta = (event["pts_seconds"] - pts_origin) * RATE
            if (
                abs(delta - round(delta)) > 0.001
                or event["global_frame"] != scene_origin + round(delta)
                or event["global_frame"] % FRAMES != event["marker"]
            ):
                report["errors"].append(
                    {"kind": "marker_transport_mismatch", "name": event["name"]}
                )
            age = (event["arrival_ns"] - epoch.value) / 1_000_000_000 - (
                event["global_frame"] + 3
            ) / RATE
            fresh_rejoin = (
                session > 0
                and event["publisher_generation"] == generation[1]
                and generation[1] > 0
                and -1 / RATE <= age <= 0.5
                and (scenario != "backpressure" or fault_finished)
            )
            event["eligible"] = (
                not (event["name"] == "main1" and fault_started and recovery.active) or fresh_rejoin
            )
            if event["name"] == "main1" and fresh_rejoin:
                recovered = recovery.observe(event)
                if recovered:
                    expected_disconnects.discard(("main1", session))
                    report["events"].append(recovered)
            report["samples"].append(event)
        elif kind == "resource":
            report["resources"].append(event)
        else:
            report["events"].append(event)
        if kind == "sent" and event["generation"] == generation[event["view"]]:
            last_sent[event["view"]] = event["sent_ns"]
        elif kind == "joined" and event["generation"] == generation[event["view"]]:
            joined_at[event["view"]] = time.monotonic_ns()
        elif kind == "observer_error":
            name = event["name"]
            expected = recovery.expects_reader_failure(
                name, event["observer_session"], expected_disconnects
            )
            if not expected:
                report["errors"].append(event)
        elif kind == "error" and not (event["view"] == 1 and recovery.active):
            report["errors"].append(event)
        elif kind == "skipped" and event["view"] == 1 and fault_started and not recovery.active:
            report["errors"].append({"kind": "post_recovery_gop_skip", "event": event})

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
                expected_disconnects.add(("main1", reader_sessions["main1"]))
                recovery.begin(generation[1])
                fault_started = True
                if scenario == "exit":
                    restart(1, "injected_exit")
                elif scenario == "stall":
                    if not hasattr(signal, "SIGSTOP"):
                        raise RuntimeError("stall experiment requires POSIX SIGSTOP")
                    process, _ = workers[1]
                    os.kill(process.pid, signal.SIGSTOP)
                else:
                    publish[1].paused.set()
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
                consume(event)
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
                if process.is_alive():
                    continue
                if not recovery.expects_reader_failure(
                    name, reader_sessions[name], expected_disconnects
                ):
                    key = (name, reader_sessions[name])
                    if key not in reported_reader_exits:
                        report["errors"].append(
                            {
                                "kind": "unexpected_reader_exit",
                                "name": name,
                                "exitcode": process.exitcode,
                            }
                        )
                        reported_reader_exits.add(key)
                    continue
                stop_process(process, stop)
                if reader_retries.get(name, 0) >= 8:
                    raise RuntimeError(f"reader {name} exhausted reconnect attempts")
                reader_retries[name] = reader_retries.get(name, 0) + 1
                launch_reader(reader_view[name], name)
    except Exception as exc:
        report["errors"].append({"kind": "experiment_error", "detail": str(exc)})
        report["verdict"] = "failed"
    finally:
        actions: list[tuple[str, Callable[[], None]]] = []
        for process, stop, proxy in readers.values():
            proxy.expected_disconnect.set()
            actions.append(
                (f"reader:{process.pid}", partial(stop_process, process, stop, timeout=3))
            )
        for process, stop in workers.values():
            actions.append(
                (f"publisher:{process.pid}", partial(stop_process, process, stop, timeout=3))
            )
        actions.extend((f"proxy:{index}", proxy.close) for index, proxy in enumerate(proxies))
        actions.append(("server", partial(stop_server, server)))
        actions.append(("server_log", server_log.close))
        cleanup_all(actions, report["errors"])
        for index, proxy in enumerate(proxies):
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
                start_times = [
                    event["at_ns"] for event in report["events"] if event["kind"] == "fault_begin"
                ]
                end_times = [
                    event["confirmed_at_ns"]
                    for event in report["events"]
                    if event["kind"] == "observed_recovery"
                ]
                expected_window = (
                    scenario != "baseline" and proxy_views[proxy] == 1 and bool(start_times)
                )
                if not expected_window or any(
                    error["at_ns"] < start_times[0] or (end_times and error["at_ns"] > end_times[0])
                    for error in proxy.errors
                ):
                    report["errors"].append({"kind": "unexpected_proxy_error", "proxy": index})
                    report["verdict"] = "failed"
        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    trailing = events.get(0.05)
                except queue.Empty:
                    break
                consume(trailing)
            report["telemetry"] = events.evidence()
            if events.gaps or report["telemetry"]["incomplete"]:
                report["errors"].append({"kind": "telemetry_incomplete"})
            report["server_log"] = (directory / "server.log").read_text()
        except Exception as exc:
            report["errors"].append({"kind": "final_evidence_error", "detail": str(exc)})
        finally:
            events.close()
    if "epoch_ns" in report:
        validate_report(report)
    else:
        report["verdict"] = "failed"
    return report


def stop_server(server: subprocess.Popen) -> None:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)


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
        report = run_bounded(args.duration, args.b_frames, args.scenario, binary, Path(directory))
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
