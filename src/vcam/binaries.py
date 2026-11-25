"""Locate, download and verify the MediaMTX server binary."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .errors import BinaryError

DEFAULT_VERSION = "v1.21.4"
RELEASE_BASE = "https://github.com/bluenviron/mediamtx/releases/download"
ENV_BINARY = "VCAM_MEDIAMTX_BIN"
ENV_CACHE = "VCAM_CACHE_DIR"


def cache_dir() -> Path:
    override = os.environ.get(ENV_CACHE)
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "vcam"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "vcam"


def platform_slug() -> str:
    """Map the running platform onto a MediaMTX release asset suffix."""
    system = platform.system().lower()
    machine = _host_machine()

    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "darwin_arm64"
        if machine in ("x86_64", "amd64"):
            return "darwin_amd64"
    elif system == "linux":
        if machine in ("aarch64", "arm64"):
            return "linux_arm64"
        if machine in ("x86_64", "amd64"):
            return "linux_amd64"
        if machine.startswith("armv7"):
            return "linux_armv7"
        if machine.startswith("armv6"):
            return "linux_armv6"
    elif system == "windows" and machine in ("x86_64", "amd64"):
        return "windows_amd64"

    raise BinaryError(f"unsupported platform for MediaMTX: {system}/{machine}")


def _host_machine() -> str:
    """Real host architecture, seeing through Rosetta on Apple Silicon."""
    machine = platform.machine().lower()
    if platform.system() == "Darwin" and machine == "x86_64" and _is_rosetta():
        return "arm64"
    return machine


def _is_rosetta() -> bool:
    try:
        result = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.decode(errors="replace").strip() == "1"


def asset_name(version: str = DEFAULT_VERSION, slug: str | None = None) -> str:
    resolved = slug or platform_slug()
    extension = "zip" if resolved.startswith("windows") else "tar.gz"
    return f"mediamtx_{version}_{resolved}.{extension}"


def asset_url(version: str = DEFAULT_VERSION, slug: str | None = None) -> str:
    return f"{RELEASE_BASE}/{version}/{asset_name(version, slug)}"


def checksums_url(version: str = DEFAULT_VERSION) -> str:
    return f"{RELEASE_BASE}/{version}/checksums.sha256"


def install_path(version: str = DEFAULT_VERSION, slug: str | None = None) -> Path:
    binary = "mediamtx.exe" if platform.system() == "Windows" else "mediamtx"
    return cache_dir() / "mediamtx" / version / (slug or platform_slug()) / binary


def resolve_binary(
    explicit: Path | None = None,
    *,
    version: str = DEFAULT_VERSION,
    allow_download: bool = True,
    on_event: Callable[[str], None] | None = None,
) -> Path:
    """Find a usable MediaMTX binary, downloading it as a last resort.

    Resolution order: explicit path, ``$VCAM_MEDIAMTX_BIN``, ``$PATH``, local cache,
    then the pinned GitHub release.
    """
    notify = on_event or (lambda _message: None)

    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise BinaryError(f"mediamtx binary not found: {candidate}")
        return candidate

    from_env = os.environ.get(ENV_BINARY)
    if from_env:
        candidate = Path(from_env).expanduser()
        if not candidate.is_file():
            raise BinaryError(f"{ENV_BINARY} points to a missing file: {candidate}")
        return candidate

    on_path = shutil.which("mediamtx")
    if on_path:
        return Path(on_path)

    cached = install_path(version)
    if cached.is_file():
        return cached

    if not allow_download:
        raise BinaryError(
            "mediamtx not found and downloads are disabled; install it, set "
            f"{ENV_BINARY}, or run `vcam install-server`"
        )

    notify(f"downloading MediaMTX {version} ({platform_slug()})")
    return download(version=version, on_event=notify)


def download(
    *,
    version: str = DEFAULT_VERSION,
    destination: Path | None = None,
    verify: bool = True,
    on_event: Callable[[str], None] | None = None,
) -> Path:
    """Download and extract the MediaMTX release binary. Returns its path."""
    notify = on_event or (lambda _message: None)
    target = destination or install_path(version)
    target.parent.mkdir(parents=True, exist_ok=True)

    name = asset_name(version)
    url = asset_url(version)

    with tempfile.TemporaryDirectory(prefix="vcam-mediamtx-") as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / name
        _fetch(url, archive)

        if verify:
            expected = _expected_checksum(version, name)
            if expected is None:
                notify(f"warning: no checksum published for {name}, skipping verification")
            else:
                actual = _sha256(archive)
                if actual != expected:
                    raise BinaryError(
                        f"checksum mismatch for {name}: expected {expected}, got {actual}"
                    )
                notify("checksum verified")

        extracted = _extract_binary(archive, tmp_dir)
        shutil.move(str(extracted), str(target))

    target.chmod(0o755)
    notify(f"installed {target}")
    return target


def _fetch(url: str, destination: Path) -> None:
    try:
        with urlopen(url, timeout=120) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise BinaryError(f"failed to download {url}: {exc}") from exc


def _expected_checksum(version: str, name: str) -> str | None:
    try:
        with urlopen(checksums_url(version), timeout=60) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError):
        return None

    for line in payload.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0]
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_binary(archive: Path, workdir: Path) -> Path:
    out_dir = workdir / "extracted"
    out_dir.mkdir(exist_ok=True)

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(out_dir)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            _safe_extract(bundle, out_dir)

    for candidate in out_dir.rglob("mediamtx*"):
        if candidate.is_file() and candidate.suffix in ("", ".exe"):
            return candidate
    raise BinaryError(f"no mediamtx binary inside {archive.name}")


def _safe_extract(bundle: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in bundle.getmembers():
        member_path = (root / member.name).resolve()
        if not str(member_path).startswith(str(root)):
            raise BinaryError(f"refusing to extract path outside archive root: {member.name}")
    bundle.extractall(destination)
