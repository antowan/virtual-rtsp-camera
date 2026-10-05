"""Attempt every owned cleanup and bound experiments from outside their process."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import time
import traceback
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path


def cleanup_all(actions: list[tuple[str, Callable[[], None]]], errors: list[dict]) -> None:
    for name, action in actions:
        try:
            action()
        except Exception as exc:
            errors.append({"kind": "cleanup_error", "resource": name, "detail": str(exc)})


def _run_owned(
    connection, duration: float, b_frames: int, scenario: str, binary: str, directory: str
) -> None:
    from .__main__ import run_experiment

    os.setsid()
    connection.send(os.getpid())
    connection.close()
    try:
        report = run_experiment(duration, b_frames, scenario, Path(binary), Path(directory))
    except Exception:
        report = {
            "verdict": "failed",
            "errors": [{"kind": "experiment_error", "detail": traceback.format_exc()}],
        }
    (Path(directory) / "result.json").write_text(json.dumps(report))


def run_bounded(
    duration: float,
    b_frames: int,
    scenario: str,
    binary: Path,
    directory: Path,
    *,
    timeout: float | None = None,
    target=None,
) -> dict:
    if not hasattr(os, "setsid"):
        raise RuntimeError("the isolated prototype runner requires POSIX process groups")
    ctx = mp.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_run_owned if target is None else target,
        args=(send, duration, b_frames, scenario, str(binary), str(directory)),
    )
    deadline = time.monotonic() + (duration + 45 if timeout is None else timeout)
    process.start()
    send.close()
    group = None
    try:
        if receive.poll(max(0, min(5, deadline - time.monotonic()))):
            with suppress(EOFError):
                announced_group = receive.recv()
                if announced_group != process.pid:
                    raise RuntimeError("owned experiment group differs from its process PID")
                group = announced_group
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive():
            return {"verdict": "failed", "errors": [{"kind": "experiment_deadline"}]}
        result = directory / "result.json"
        if process.exitcode != 0 or not result.exists():
            return {
                "verdict": "failed",
                "errors": [{"kind": "experiment_process_failed", "exitcode": process.exitcode}],
            }
        return json.loads(result.read_text())
    finally:
        receive.close()
        # The child established a dedicated session before starting any helpers.
        # Kill its exact group, never a process-name match or the caller's group.
        if group is not None:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
            process.join(1)
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        elif process.is_alive():
            process.kill()
        process.join(3)
        if process.is_alive():
            raise RuntimeError("owned experiment runner did not stop")
