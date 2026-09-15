# Changelog

All notable changes to **vcam** are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- ─────────────────────────────────────────────────────────────────────────
  HOW TO ADD A RELEASE ENTRY
  ──────────────────────────
  1. Copy the [Unreleased] block, replace "Unreleased" with the new version and
     today's date: ## [1.2.3] - 2025-06-15
  2. Clear the copied [Unreleased] block, ready for the next cycle.
  3. Add a link at the bottom of the file.
  4. Tag the commit: git tag v1.2.3 && git push --tags
     → the Release workflow fires, extracts this block, and creates the GitHub
       Release with the wheel attached.

  Only include headings that have entries.  Standard headings:
    ### Added        — new features visible to users
    ### Changed      — changes to existing behaviour
    ### Deprecated   — features to be removed in a future version
    ### Removed      — features removed in this version
    ### Fixed        — bug fixes
    ### Security     — vulnerability fixes
──────────────────────────────────────────────────────────────────────────── -->

## [Unreleased]

### Added

-

---

## [0.3.0] - 2026-09-15

### Added

- Optional Toxiproxy Docker Compose profile for TCP network chaos testing,
  including runtime latency, bandwidth, timeout, and disconnect scenarios.
- `vcam doctor` reports the optional Toxiproxy CLI or API when available.

---

## [0.2.0] - 2026-08-30

### Added

- `vcam replay` serves a captured RTSP/RTP session back byte for byte, over
  interleaved TCP or UDP, preserving the original payloads and inter-packet
  timing. Captures are recorded with `tcpdump`; see the README for the workflow.

### Changed

- The capture size limit is now 256 MB, down from 512 MB. Loading a capture
  costs about 6x the file size in memory at roughly 15 MB/s, so the old limit
  quietly allowed a ~3 GB resident set. The refusal message states the memory a
  load would need.
- Replay warns when the host could not keep pace with the capture. Late packets
  were counted but never reported, and absolute deadlines keep the stream in
  sync regardless, so the operator had no way to see it.

### Fixed

- `--sdp` descriptions were modified in place while a capture was being
  matched, dropping any media block the capture had no stream for.
- `vcam --version` reported the wrong version. It was hardcoded in
  `vcam/__init__.py` and had drifted two releases behind `pyproject.toml`; it is
  now read from the installed distribution.
- `ReplayServer.shutdown()` could return while a reader was still being sent
  RTP. Two separate races: callers of `stop_player` after the first returned
  without waiting, and a connection was unregistered before it was closed, so
  shutdown could not see it.
- A capture played with `--no-loop` never hung up when it ran out, leaving
  clients waiting on a silent connection.
- The MediaMTX API port preflight probed `0.0.0.0` while MediaMTX binds
  `127.0.0.1`, so a taken port looked free and startup failed later with a
  message blaming the RTSP port.

### Security

- Container security updates were being served from a stale build cache, so
  `apt-get upgrade` had frozen at the packages current when the cache was
  written. The layer is now keyed on the build date and refreshes daily.

---

## [0.1.4] - 2026-08-26

### Added

- `vcam generate` — interactive wizard that scans a directory for video files
  and guides the user through building a `cameras.yaml` without writing YAML by
  hand. For each discovered file it asks whether to include it, confirms or
  overrides the camera name (auto-derived from the file stem), probes the file
  with ffprobe to recommend `copy` vs `transcode`, and accepts a start offset.
  Server settings (RTSP port, optional auth) are collected at the end. Supports
  `--scan <dir>` to target a specific folder and `--force` to overwrite an
  existing config.

---

## [0.1.3] - 2026-08-25

### Added

- Repository governance baseline for public collaboration with low maintainer
  overhead:
  - MIT `LICENSE`
  - `CONTRIBUTING.md` with scope boundaries and issue-first policy
  - `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)
  - GitHub issue templates for bug reports and feature requests
  - pull request template with focused review checklist
  - stale workflow to auto-manage inactive issues/PRs
- README badges and a contribution policy summary to improve discoverability and
  set contribution expectations on the landing page.

---

## [0.1.2] - 2026-08-25

### Added

- Demo entrypoint for zero-config Docker usage:
  `scripts/entrypoint.sh` now auto-generates a ready-to-run `cameras.yaml` from
  mounted sample videos when no config is provided, so container users can start
  streaming with minimal setup.

### Changed

- Bump GitHub Actions to current majors via Dependabot (#5):
  `actions/checkout` v4 -> v7, `actions/upload-artifact` v4 -> v7,
  `actions/download-artifact` v4 -> v8, `astral-sh/setup-uv` v5 -> v7,
  `docker/setup-qemu-action` v3 -> v4, `docker/setup-buildx-action` v3 -> v4,
  `docker/login-action` v3 -> v4, `docker/build-push-action` v6 -> v7,
  `docker/metadata-action` v5 -> v6, `softprops/action-gh-release` v2 -> v3.
- Bump `typer` 0.27.0 -> 0.27.1 via Dependabot (#3). This also raised the
  declared floor from `typer>=0.12` to `typer>=0.27.1`.

---

## [0.1.1] - 2026-08-25

### Security

- Bump the bundled MediaMTX from `v1.19.3` to `v1.20.1`. The v1.19.3 binary was
  built with Go 1.26.5 and carried 9 fixable HIGH advisories in the Go standard
  library (CVE-2026-39821, -33818, -46600, -56853, -56858, -56859, -56860,
  -56862) plus CVE-2026-71556 in `go-git`; v1.20.1 is built with Go 1.26.6.
- Runtime image: `apt-get upgrade` during build so `trixie-security` updates
  published after the base image was cut are picked up.
- Runtime image: remove `perl-base`. Nothing in the image needs it (ffmpeg and
  CPython both verified working without it) and it shipped CVE-2026-12087,
  -13221 and -48959, none of which have a fixed package in Debian trixie.
- Runtime image: uninstall `pip` after the wheel is installed. It is unused at
  runtime and carried 7 known advisories.
- CI: Docker builds now run with `pull: true` so a cached stale base image
  cannot silently reintroduce patched vulnerabilities.

  Net effect: fixable CRITICAL/HIGH findings in the published image drop from
  9 to 0.

  Known accepted risk: `libcjson1` CVE-2026-67215 / -29036 / -67216 remain.
  The package is a hard dependency of `librist4`, which Debian's (and Alpine's)
  `ffmpeg` is linked against, so it cannot be removed without a custom ffmpeg
  build. No fixed version is available upstream.

### Added

- `.github/dependabot.yml` — weekly update checks for uv, Docker and GitHub
  Actions dependencies.
- CI now fails on *fixable* CRITICAL/HIGH image vulnerabilities via Trivy.
  Unfixable findings are ignored so the pipeline is not permanently blocked.
- Publish wheel to GitHub Packages (PyPI registry) on every release.
  (Removed later in this same release — see Fixed.)
- Push `konekuto/vcam:main` to Docker Hub on every merge to `main`.

### Fixed

- CI: pinned the Trivy scan step to `aquasecurity/trivy-action@v0.36.0`; the
  previously referenced `@0.28.0` was missing the `v` prefix and could not be
  resolved, failing every Docker build job.
- CI release pipeline: removed the "Publish to GitHub Packages" job. GitHub
  Packages has no PyPI registry (only npm, RubyGems, Maven, Gradle, Docker and
  NuGet), so `https://pypi.pkg.github.com/...` always returned 404. Because
  `release` declared `needs: [build, publish, docker]`, that failure skipped
  the "Create GitHub Release" job and blocked every release. The wheel and
  sdist are still attached to the GitHub Release as assets.
- CI release pipeline: fixed duplicate workflow content causing YAML parse error.
- CI release pipeline: release body now written via Python to avoid shell
  interpolation of backtick-quoted text in CHANGELOG entries.
- CI: Docker Hub push jobs now reference the `dockerhub` environment so
  `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets are correctly injected.


---

## [0.1.0] - 2025-08-25

### Added

- `vcam run` — start one or more looping RTSP cameras from local video files,
  multiplexed over a single port via MediaMTX (auto-downloaded, SHA-256 verified).
- `vcam init / add / list / urls / show` — configuration file management
  (`cameras.yaml`).
- `vcam probe` — inspect a video file and show which stream mode `auto` would pick.
- `vcam doctor` — check that ffmpeg, ffprobe and MediaMTX are present.
- `vcam install-server` — pre-fetch the MediaMTX binary into the local cache.
- `vcam service install / start / stop / status / uninstall / logs` — register
  the stack as a **systemd user unit** (Linux) or **launchd LaunchAgent** (macOS),
  no root required.
- `python -m vcam` entry-point so service units work even when `vcam` is not on
  the system `PATH`.
- Fault-simulation modes: `noise`, `degraded`, `frozen`, `blackout`, `flaky`,
  `stutter` (useful for testing downstream analytics pipelines).
- Linux `aarch64` support (downloads the `linux_arm64` MediaMTX build).
- macOS Apple Silicon support with Rosetta detection.

---

<!-- version diff links ────────────────────────────────────────────────────── -->
[Unreleased]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/antoine-em/virtual-rtsp-camera/releases/tag/v0.1.0
