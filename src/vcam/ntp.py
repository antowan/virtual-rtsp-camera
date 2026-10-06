"""NTP client and clock synchronisation for container deployments.

This module is intentionally limited to the Python standard library.

Clock adjustment is only safe when the process runs in an isolated VM, such as
the Linux VM used by Docker Desktop, or when changing the host clock is
explicitly intended. Native Linux containers share the host kernel clock;
``CAP_SYS_TIME`` does not provide a per-container clock. Calling
:func:`apply_offset` on a bare system or inside a native Linux container may
affect the whole machine.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import socket
import struct
import time
from pathlib import Path

from .errors import NTPError

#: Seconds between the NTP epoch (1 Jan 1900) and the Unix epoch (1 Jan 1970).
_NTP_DELTA = 2_208_988_800

#: Above this absolute offset, use a clock_settime *step* instead of adjtimex
#: *slew*.  128 ms matches the default ntpd step threshold.
_STEP_THRESHOLD = 0.128


# ---------------------------------------------------------------------------
# Container / capability detection
# ---------------------------------------------------------------------------


def running_in_container() -> bool:
    """Return True when the process is running inside a Docker/OCI container.

    Checks the Docker sentinel file ``/.dockerenv`` first, then falls back to
    inspecting the cgroup hierarchy for Docker/containerd markers.
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
        return "docker" in cgroup or "containerd" in cgroup
    except OSError:
        return False


def has_sys_time_cap() -> bool:
    """Return True if the process holds ``CAP_SYS_TIME`` (bit 25 of CapEff).

    This capability is required to call ``adjtimex(2)`` and ``clock_settime(2)``.
    Adding ``cap_add: [SYS_TIME]`` may allow changing the host clock on native Linux.
    """
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("CapEff:"):
                cap_eff = int(line.split(":")[1].strip(), 16)
                return bool(cap_eff & (1 << 25))  # CAP_SYS_TIME = 25
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# NTP measurement
# ---------------------------------------------------------------------------


def measure_offset(
    server: str,
    port: int = 123,
    samples: int = 3,
    timeout: float = 3.0,
) -> tuple[float, float]:
    """Query *server* and return ``(offset_seconds, rtt_seconds)``.

    Sends *samples* NTPv3 mode-3 requests and returns the measurement with the
    smallest RTT, which best satisfies the symmetric-path assumption and gives
    the most accurate offset estimate.

    Raises :class:`NTPError` if every attempt fails.
    """
    msg = b"\x1b" + 47 * b"\x00"  # NTPv3 client request (LI=0, VN=3, Mode=3)
    best_rtt = float("inf")
    best_offset = 0.0
    last_exc: Exception | None = None

    for _ in range(samples):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                t1 = time.time()
                sock.sendto(msg, (server, port))
                data, _ = sock.recvfrom(1024)
                t4 = time.time()

            # Transmit Timestamp is at bytes 40–47 (seconds + fractions, big-endian)
            tx_s, tx_f = struct.unpack("!II", data[40:48])
            ntp_time = tx_s - _NTP_DELTA + tx_f / 2**32
            rtt = t4 - t1
            # Best-estimate offset: midpoint correction for network delay
            offset = ntp_time - (t1 + rtt / 2)

            if rtt < best_rtt:
                best_rtt, best_offset = rtt, offset
        except OSError as exc:
            last_exc = exc

    if best_rtt == float("inf"):
        raise NTPError(f"NTP query to {server}:{port} failed: {last_exc}") from last_exc

    return best_offset, best_rtt


# ---------------------------------------------------------------------------
# Clock adjustment
# ---------------------------------------------------------------------------


def apply_offset(offset_seconds: float) -> None:
    """Adjust the visible system clock by *offset_seconds*. Requires ``CAP_SYS_TIME``.

    Uses ``clock_settime(2)`` (instant step) for ``|offset| > 128 ms``, and
    ``adjtimex(2)`` (gradual slew) for smaller corrections.  The slew avoids
    a discontinuity in the NTP timestamps embedded in RTCP Sender Reports
    while the stream is live. On native Linux, this can change the host clock;
    use only in an isolated VM or when that effect is intended.

    Raises :class:`OSError` if the syscall fails (e.g. missing capability).
    """
    if abs(offset_seconds) > _STEP_THRESHOLD:
        _clock_settime_step(offset_seconds)
    else:
        _adjtimex_slew(offset_seconds)


def _clock_settime_step(offset_seconds: float) -> None:
    """Instantly step ``CLOCK_REALTIME`` by *offset_seconds*."""
    _CLOCK_REALTIME = 0

    class _Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    libc = _libc()
    new_time = time.time() + offset_seconds
    ts = _Timespec(tv_sec=int(new_time), tv_nsec=int((new_time % 1) * 1_000_000_000))
    ret = libc.clock_settime(_CLOCK_REALTIME, ctypes.byref(ts))
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"clock_settime failed (errno {errno})")


def _adjtimex_slew(offset_seconds: float) -> None:
    """Slew ``CLOCK_REALTIME`` by *offset_seconds* via ``adjtimex(ADJ_SETOFFSET)``."""
    _ADJ_SETOFFSET = 0x0100  # apply a one-time offset
    _ADJ_NANO = 0x2000  # offset field is in nanoseconds

    # struct timex layout for 64-bit Linux (from <sys/timex.h>)
    class _Timex(ctypes.Structure):
        _fields_ = [
            ("modes", ctypes.c_uint),
            ("offset", ctypes.c_long),
            ("freq", ctypes.c_long),
            ("maxerror", ctypes.c_long),
            ("esterror", ctypes.c_long),
            ("status", ctypes.c_int),
            ("constant", ctypes.c_long),
            ("precision", ctypes.c_long),
            ("tolerance", ctypes.c_long),
            ("time_sec", ctypes.c_long),
            ("time_usec", ctypes.c_long),
            ("tick", ctypes.c_long),
            ("ppsfreq", ctypes.c_long),
            ("jitter", ctypes.c_long),
            ("shift", ctypes.c_int),
            ("stabil", ctypes.c_long),
            ("jitcnt", ctypes.c_long),
            ("calcnt", ctypes.c_long),
            ("errcnt", ctypes.c_long),
            ("stbcnt", ctypes.c_long),
            ("tai", ctypes.c_int),
            ("_pad", ctypes.c_int * 11),
        ]

    offset_ns = int(offset_seconds * 1_000_000_000)
    tx = _Timex()
    tx.modes = _ADJ_SETOFFSET | _ADJ_NANO
    # ADJ_SETOFFSET uses time_sec / time_usec (despite the name, nanoseconds when ADJ_NANO is set)
    tx.time_sec = offset_ns // 1_000_000_000
    tx.time_usec = abs(offset_ns) % 1_000_000_000

    libc = _libc()
    ret = libc.adjtimex(ctypes.byref(tx))
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"adjtimex failed (errno {errno})")


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if name is None:
        raise OSError("libc not found")
    return ctypes.CDLL(name, use_errno=True)
