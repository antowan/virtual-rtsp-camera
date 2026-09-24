# virtual-rtsp-camera

![CI](https://img.shields.io/github/actions/workflow/status/antowan/virtual-rtsp-camera/ci.yml?branch=main&label=build)
![Release](https://img.shields.io/github/v/release/antowan/virtual-rtsp-camera)
![License](https://img.shields.io/github/license/antowan/virtual-rtsp-camera)
![Last Commit](https://img.shields.io/github/last-commit/antowan/virtual-rtsp-camera)
![Issues](https://img.shields.io/github/issues/antowan/virtual-rtsp-camera)
[![Docker Pulls](https://img.shields.io/docker/pulls/konekuto/vcam)](https://hub.docker.com/r/konekuto/vcam)
[![Docker Image Size](https://img.shields.io/docker/image-size/konekuto/vcam/latest)](https://hub.docker.com/r/konekuto/vcam)

`vcam` — a command line tool and Docker image that turns local video files into
**looping virtual RTSP camera streams**, so video analytics pipelines (such as NVIDIA
DeepStream, Frigate, or any RTSP-based CCTV/NVR software) can be developed and tested
without physical IP cameras.

```bash
docker run --rm -p 8554:8554 \
  -v "$PWD/cameras.yaml:/vcam/cameras.yaml:ro" \
  -v "$PWD/videos:/vcam/videos:ro" \
  konekuto/vcam run
```

Multi-arch image on Docker Hub: [`konekuto/vcam`](https://hub.docker.com/r/konekuto/vcam)
(amd64 + arm64, including Jetson/aarch64 edge devices).

- Multiple cameras are served from **one RTSP port**, one path per camera
  (`rtsp://host:8554/cam1`, `rtsp://host:8554/cam2`, …).
- A camera can claim its **own port** when you need to simulate physically separate devices.
- **No authentication by default**; add `--username/--password` to require credentials from readers.
- Sources loop forever, paced in real time, and are restarted automatically if a publisher dies.
- **Packet captures from real cameras can be replayed byte for byte**, when a synthetic
  feed is not faithful enough (see [Capture replay](#capture-replay)).

Under the hood the tool runs a [MediaMTX](https://github.com/bluenviron/mediamtx) server
(auto-downloaded for your architecture, SHA-256 verified) and one `ffmpeg` publisher per camera.
Capture replay bypasses both and serves recorded packets from its own RTSP server, so nothing
re-packetises the stream on the way out.

## Requirements

- Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on `PATH`
- MediaMTX — resolved automatically, or install it yourself (see [Server binary](#server-binary))

## Install

```bash
uv sync                 # dev install into ./.venv
uv run vcam --help
```

Capture replay needs `scapy`, which is an optional extra so the rest of the CLI does not
carry it:

```bash
uv sync --extra replay          # or: pip install 'vcam[replay]'
```

Or install the CLI globally:

```bash
uv tool install .
vcam --help
```

Check your environment at any time:

```bash
uv run vcam doctor
```

## Quick start

One camera from one file:

```bash
uv run vcam run --source videos/cam1.mp4
# -> rtsp://127.0.0.1:8554/cam1
```

Several cameras on the same port, different paths:

```bash
uv run vcam run \
  --camera cam1=videos/cam1.mp4 \
  --camera cam2=videos/cam2.mp4 \
  --camera cam3=videos/cam3.mp4
# -> rtsp://127.0.0.1:8554/cam1
#    rtsp://127.0.0.1:8554/cam2
#    rtsp://127.0.0.1:8554/cam3
```

With credentials (readers must authenticate, local publishers still don't):

```bash
uv run vcam run -s videos/cam1.mp4 --username reader --password s3cret
# -> rtsp://reader:s3cret@127.0.0.1:8554/cam1
```

Read a stream back:

```bash
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
ffplay  -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
```

## Video handling modes

`--mode` controls what happens between the file and the wire:

| mode | behaviour |
| --- | --- |
| `auto` *(default)* | probe the file; **copy** when it is already H.264/HEVC, otherwise **transcode** |
| `copy` | pure passthrough — just read the video and stream it, no re-encode, near-zero CPU |
| `transcode` | re-encode with the resolution / fps / bitrate / codec / GOP you ask for |

```bash
# passthrough, cheapest
uv run vcam run -s videos/cam1.mp4 --mode copy

# force a 720p15 2 Mbit/s H.264 feed regardless of the source
uv run vcam run -s videos/cam1.mp4 \
  --mode transcode --resolution 1280x720 --fps 15 --bitrate 2M --gop 30
```

Inspect a file and see which mode `auto` would pick:

```bash
uv run vcam probe videos/cam1.mp4
```

Other per-camera knobs: `--loop/--no-loop`, `--realtime/--no-realtime` (drop `-re` pacing
to push as fast as possible), `--start-offset` (seek into the file so feeds are de-synced),
`--transport tcp|udp`, `--audio/--no-audio`, and `--encoder` for hardware encoders
(e.g. `--encoder h264_nvenc` on hardware that supports it).

## Simulation modes

Cameras are clean by default. `--simulation` makes one misbehave, so you can point a
consumer at a camera that is broken in a specific, repeatable way instead of waiting
for the real thing to fail:

| mode | what a reader sees |
| --- | --- |
| `normal` *(default)* | the feed, untouched |
| `noise` | heavy sensor grain, as from a badly-lit or failing sensor |
| `degraded` | starved bitrate and rare keyframes — blocking, smearing, slow recovery |
| `frozen` | the picture updates once a second and never catches up |
| `blackout` | a black but perfectly valid stream: a dead sensor, not a dead link |
| `flaky` | the stream **drops entirely** every `interval`, for `duration` |
| `stutter` | the picture **freezes** for `duration` every `interval`, then jumps ahead |

```bash
# a noisy camera
uv run vcam run -s videos/cam1.mp4 --simulation noise --noise-level 60

# drops off the network for 5s every 30s
uv run vcam run -s videos/cam1.mp4 --simulation flaky \
  --simulation-interval 30 --simulation-duration 5
```

Per camera in YAML:

```yaml
cameras:
  - name: grainy
    source: videos/cam1.mp4
    simulation:
      mode: noise
      noise_level: 60        # 1-100, default 30

  - name: dropout
    source: videos/cam2.mp4
    simulation:
      mode: flaky
      interval: 45           # seconds of healthy stream between events
      duration: 5            # how long each event lasts

  - name: laggy
    source: videos/cam3.mp4
    simulation:
      mode: stutter
      interval: 12
      duration: 6

  - name: custom
    source: videos/cam4.mp4
    simulation:
      filters: gblur=sigma=4  # any ffmpeg filter chain, applied on top
```

Notes worth knowing:

- **Everything but `flaky` happens inside one ffmpeg filter graph.** The publisher is
  never swapped mid-run, so a reader attached across a `stutter` freeze keeps receiving
  frames and recovers on its own. `flaky` is the exception by design: the stream really
  does go away, and readers really do get disconnected.
- A simulation that has to touch pixels **forces a transcode**, even on an H.264 source
  that would otherwise be passthrough. `vcam list` and the health file report the mode
  that actually runs.
- `noise` is worst-case for an encoder, so it is capped at 4 Mbit/s unless the camera
  sets its own `bitrate`; `degraded` defaults to 150 kbit/s with a 300-frame GOP. Any
  explicit `video.bitrate` / `video.gop` always wins.
- `flaky` dropouts are *planned*: they do not count as crashes, so they never trip the
  restart backoff or `--max-restarts`. The health file reports `simulation_state`
  (`up` / `down`) for those cameras.

## Capture replay

Simulation modes fake a *class* of fault. Sometimes you need the exact one: the RTP
stream that made a specific decoder fall over on a specific site, on a Tuesday. For that,
capture the real camera once and replay the packets.

```bash
# 1. capture the camera's traffic (on a host that sees it)
sudo tcpdump -i eth0 -s 0 -w camera.pcap host 192.168.1.64 and tcp port 554

# 2. inspect what is in there
uv run vcam replay camera.pcap --list-tracks

# 3. serve it
uv run vcam replay camera.pcap --port 8600 --path cam1
# -> rtsp://127.0.0.1:8600/cam1
```

Replay does **not** go through MediaMTX or ffmpeg. `vcam` serves the capture from its own
minimal RTSP server and puts the recorded packets on the wire untouched — same payload
bytes, same SSRC, same sequence numbers, same fragmentation, same inter-packet timing.
Anything that re-packetises the stream (MediaMTX included) would sanitise away exactly the
packet-level artifacts you captured it for.

What a capture needs to contain:

- **The RTSP handshake**, ideally. `vcam` reads the `DESCRIBE` response for the SDP and the
  `SETUP` exchanges for the RTP ports or interleaved channels. For UDP, it matches packets to
  the negotiated source and destination endpoints, so unrelated traffic is not replayed.
- If you only have the media, pass the session description yourself with
  `--sdp camera.sdp`; `vcam` then finds the RTP streams heuristically.

What replay deliberately does **not** do:

- **No `PAUSE`.** It answers `501`: resuming would restart the timeline with backwards RTP
  counters, which is the discontinuity looping exists to avoid. Stop and reconnect instead.
- **No seeking**, and no RTCP Sender Reports — so a multi-track capture carries no
  RTP-to-wall-clock mapping for A/V sync.
- **No session enforcement.** Any reader on a connection can drive it; this is a lab tool.
- **No huge captures.** Loading materialises the packets several times over — measured at
  roughly **6× the file size in RAM, at ~15 MB/s** — so anything above 256 MB (about 1.5 GB
  of memory) is refused rather than risking an OOM kill. Trim it (`editcap -A/-B`, or a
  tighter `tcpdump` filter), or raise `$VCAM_MAX_CAPTURE_BYTES` if you have the headroom.

Both transports are handled: RTP-over-UDP and `RTP/AVP/TCP` interleaved inside the RTSP
connection (which is what most IP cameras actually use). Readers choose their own transport
independently of how the capture was made.

| flag | what it does |
| --- | --- |
| `--port` | port for this replay's own listener — **required to be free**; a replay never shares the MediaMTX port |
| `--path` | RTSP path (defaults to the capture's file stem) |
| `--loop` / `--no-loop` | repeat forever (default) or serve once and stop |
| `--rewrite-on-loop` | keep sequence numbers and RTP timestamps monotonic across loops (default) |
| `--speed` | playback multiplier; `2.0` replays twice as fast |
| `--sdp` | session description to use when the capture has no `DESCRIBE` |
| `--username` / `--password` | require basic auth from readers (or set the secret in `$VCAM_REPLAY_PASSWORD`, which is how `vcam run` passes it so it never reaches the process list) |
| `--list-tracks` | describe the capture and exit without serving |

**Looping rewrites headers on purpose.** A raw rewind sends sequence numbers and
timestamps backwards, and decoders stall on it (`ffmpeg` reports *non monotonically
increasing dts*). By default each loop is offset past the previous one, so the stream runs
indefinitely while the *packets themselves* stay byte-identical. Real loss gaps inside the
capture are preserved. Use `--no-rewrite-on-loop` when the rewind is the thing you want to
reproduce.

> **Captures contain credentials.** RTSP `Authorization` headers and `rtsp://user:pass@host`
> URIs are recorded in plain text. `vcam replay --list-tracks` warns when it finds them.
> Strip them before sharing a capture: `editcap -E 0 in.pcap out.pcap`, or re-capture
> against a camera with throwaway credentials.

In YAML, replays sit next to cameras and each one needs its own port:

```yaml
replays:
  - name: broken-cam
    source: captures/broken-cam.pcap   # relative paths resolve next to this file
    port: 8600               # own listener; must not collide with a camera port
    loop: true
    # sdp: captures/broken-cam.sdp   # only if the capture has no DESCRIBE
    # speed: 1.0
    # rewrite_on_loop: true
    # enabled: true
```

`vcam run` starts replays alongside cameras, and `vcam list` / `vcam urls` / the health file
report them. A stack of only replays starts no MediaMTX at all.

## Network chaos testing (Toxiproxy)

While simulation modes fake video-level artifacts (such as noise or freezes), you can test
**network-layer transport failures** (latency spikes, bandwidth starvation, silent TCP disconnects)
by running an optional [Toxiproxy](https://github.com/shopify/toxiproxy) sidecar.

```
+-------------+                  +-------------+                  +-------------+
| RTSP Client | --(RTSP/TCP)-->  |  Toxiproxy  | --(RTSP/TCP)-->  |    vcam     |
|  (Consumer) |  rtsp://...:8654 |  (Port 8654)|  vcam:8554       | (MediaMTX)  |
+-------------+                  +-------------+                  +-------------+
                                        ^
                                        | HTTP API (:8474)
                                 [ QA / CI script ]
```

### 1. Start the chaos profile

```bash
docker compose --profile chaos up -d
```

This starts `vcam` on `:8554` and Toxiproxy with its HTTP API on `:8474`.

### 2. Create the RTSP proxy

Point Toxiproxy on `:8654` to upstream `vcam:8554` via `curl` (or `toxiproxy-cli`):

```bash
curl -s -X POST http://localhost:8474/proxies \
  -H "Content-Type: application/json" \
  -d '{"name": "rtsp_cam", "listen": "0.0.0.0:8654", "upstream": "vcam:8554"}'
```

Now connect your RTSP consumer or pipeline to `rtsp://localhost:8654/cam1`.

### 3. Inject fault scenarios (toxics)

Toxics can be injected and removed dynamically while clients are streaming:

- **Latency & jitter:**
  ```bash
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam/toxics \
    -H "Content-Type: application/json" \
    -d '{"type": "latency", "name": "lag", "attributes": {"latency": 500, "jitter": 100}}'
  ```
- **Bandwidth rate limit (KB/s):**
  ```bash
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam/toxics \
    -H "Content-Type: application/json" \
    -d '{"type": "bandwidth", "name": "throttle", "attributes": {"rate": 100}}'
  ```
- **Silent timeout (stalled connection):**
  ```bash
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam/toxics \
    -H "Content-Type: application/json" \
    -d '{"type": "timeout", "name": "stall", "attributes": {"timeout": 0}}'
  ```
- **Abrupt disconnect / reconnect test:**
  ```bash
  # Close connections immediately:
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam -d '{"enabled": false}'
  # Re-enable proxy to allow client reconnection:
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam -d '{"enabled": true}'
  ```

### 4. Reset or clean up toxics

```bash
# Remove a single toxic:
curl -s -X DELETE http://localhost:8474/proxies/rtsp_cam/toxics/lag

# Reset all proxies and clear all toxics:
curl -s -X POST http://localhost:8474/reset
```

## Configuration file

For anything beyond a couple of cameras, use a YAML file:

```bash
uv run vcam generate                      # interactive wizard — scan cwd, write ./cameras.yaml
uv run vcam generate -d videos/           # scan a specific folder
uv run vcam init                          # writes a template ./cameras.yaml
uv run vcam init -s a.mp4 -s b.mp4        # ...seeded with two cameras
uv run vcam add videos/cam2.mp4 -n cam2   # append a camera to it
uv run vcam show                          # print the resolved config
uv run vcam list                          # table of cameras and their URLs
uv run vcam urls                          # one URL per line, for scripts
uv run vcam run                           # picks up ./cameras.yaml automatically
```

```yaml
server:
  host: 0.0.0.0          # bind address of the RTSP listener
  rtsp_port: 8554        # shared port for every camera
  api_port: 9997         # MediaMTX HTTP API, bound to loopback only
  log_level: warn        # error | warn | info | debug
  # auth:                # omit for anonymous access (the default)
  #   username: reader
  #   password: s3cret

cameras:
  - name: cam1           # becomes the RTSP path
    source: videos/cam1.mp4  # relative paths resolve next to this file
    mode: auto                 # auto | copy | transcode
    loop: true
    realtime: true
    start_offset: 0            # seconds to seek into the file
    transport: tcp             # publishing transport
    audio: false

  - name: cam2
    source: videos/cam2.mp4
    start_offset: 12           # de-sync this feed from the others
    mode: transcode
    video:
      codec: h264              # h264 | h265
      resolution: 1280x720
      fps: 15
      bitrate: 2M
      gop: 30
      preset: veryfast
      # encoder: h264_nvenc    # explicit ffmpeg encoder, overrides `codec`

  - name: cam3
    source: videos/cam3.mp4
    port: 8555                 # own port -> its own server instance
    enabled: true
    simulation:                # omit entirely for a clean feed
      mode: normal             # see "Simulation modes" above
```

Command line flags override the file for **every** camera, which is handy for
experiments:

```bash
uv run vcam run -c cameras.yaml --mode transcode --resolution 640x360 --fps 10
```

Legacy `streams.yaml` manifests (`streams:` with `offset_seconds`) are accepted too:

```bash
uv run vcam list -c streams.yaml
```

### How `start_offset` behaves

`start_offset` is applied **once, at startup**: the camera skips into the file, and every
later loop replays the file from the beginning. The feed therefore stays permanently
phase-shifted from its peers, which is the point — it stops several cameras fed from the
same clip showing identical frames.

In `copy` mode the seek still has to land on a keyframe, so it snaps to the nearest one.
With a file encoded at the default GOP of 250 frames that quantises the offset to ~10 s
steps. If you need precise offsets, either re-encode the source with frequent keyframes:

```bash
ffmpeg -i source.mp4 -c:v libx264 -g 25 -keyint_min 25 -sc_threshold 0 -c:a copy short-gop.mp4
```

or set `mode: transcode` on that camera, where the seek is frame accurate.

## Clock synchronisation (RTCP NTP timestamps)

Every RTCP Sender Report carries a wall-clock NTP timestamp, which is what the downstream pipeline (e.g. EAIS DeepStream) uses for clock-sync diagnostics.  That timestamp comes directly from the host system clock — there is no independent clock inside vcam.

### RTCP clock chain

```
host OS clock  →  MediaMTX time.Now()  →  RTCP SR NTPTime  →  downstream client
```

### Syncing the container to an NTP server

When running inside Docker, the container has its **own Linux kernel clock** (separate from the macOS host on Docker Desktop) that can be independently adjusted with `CAP_SYS_TIME`.

```yaml
# docker-compose.yml
services:
  vcam:
    image: vcam:latest
    cap_add:
      - SYS_TIME     # grants adjtimex / clock_settime inside the container
    command: run --ntp-server 192.168.198.151   # EAIS station IP
```

Or via the config file:

```yaml
server:
  ntp_server: 192.168.198.151   # sync before start; container + SYS_TIME required
```

Before the RTSP server starts, vcam queries the NTP server (pure Python, no extra dependencies), measures the offset, and applies it:
- **|offset| ≤ 128 ms** → gradual slew via `adjtimex(ADJ_SETOFFSET)` (no timestamp jump on live streams)
- **|offset| > 128 ms** → instant step via `clock_settime`

### Checking clock status

```bash
# Read-only — works anywhere, no privileges needed
vcam clock-status --ntp-server 192.168.198.151
# System time  : 2026-08-25T10:08:34 UTC
# In container : yes
# CAP_SYS_TIME : yes
# NTP server   : 192.168.198.151
# Offset       : +1.853 ms
# RTT          : 0.812 ms
```

### Why NTP sync is container-only

On a bare CLI or systemd service the system clock is shared with the rest of the machine.  Adjusting it would affect every other process, so `--ntp-server` is rejected outside a container.  Use the host's existing NTP daemon (chrony / timesyncd) if you need whole-system sync.

### Testing clock skew impact

| Scenario | Setup |
|---|---|
| Well-synced camera | `--ntp-server <eais-ip>` + `cap_add: [SYS_TIME]` |
| Skewed camera | Disable NTP in the container (`timedatectl set-ntp false`) |
| Fixed offset | `timedatectl set-time` inside the container after disabling NTP |
| Free-running drift | Leave the container clock unsynced with no NTP daemon |



Default is **one port, one path per camera**. It keeps firewall rules simple, needs a
single server process, and matches how real NVRs expose channels.

Set `port:` on a camera only when you need to emulate separate physical devices. Cameras
are then grouped by port and one MediaMTX instance is spawned per group, each with its own
loopback API port.

## Authentication

Without `auth`, everything is anonymous: any client can read, and publishing is open.

With `auth`, readers must present the credentials, while anonymous publishing is
restricted to loopback — so the `ffmpeg` publishers spawned by `vcam` keep working without
carrying a password, and nothing outside the host can inject a stream.

## Operating the running stack

```bash
uv run vcam run --health-file /tmp/vcam-health.json   # JSON snapshot refreshed every 5s
uv run vcam run --work-dir .vcam                      # keep the generated mediamtx.yml files
uv run vcam run --dry-run                             # print the plan, start nothing
uv run vcam run -v                                    # debug logging
```

Publishers that exit are restarted with exponential backoff (1s → 30s); `--max-restarts`
caps that. Scheduled `flaky` dropouts are exempt — they are planned stops, not crashes.
`Ctrl-C` stops the publishers and then the servers.

## Server binary

Resolution order: `--mediamtx-binary` → `$VCAM_MEDIAMTX_BIN` → `mediamtx` on `PATH` →
local cache → download from GitHub releases (checksum-verified).

```bash
uv run vcam install-server                     # pre-fetch into the cache
uv run vcam run --no-download                  # never reach the network
VCAM_CACHE_DIR=/opt/vcam uv run vcam install-server
```

## Running as a service

`vcam service` installs the stack as a long-running background service — no root required.

| Platform | Backend | Unit file |
|---|---|---|
| Linux | systemd (user session) | `~/.config/systemd/user/vcam.service` |
| macOS | launchd (LaunchAgent) | `~/Library/LaunchAgents/vcam.plist` |

```bash
# 1. Create a config if you don't have one yet
vcam init -s videos/cam1.mp4

# 2. Install and start immediately
vcam service install                  # uses ./cameras.yaml by default
vcam service install -c /abs/path/cameras.yaml   # explicit config

# 3. Day-to-day operations
vcam service status
vcam service stop
vcam service start
vcam service logs                     # streams the log (Ctrl-C to exit)
vcam service uninstall
```

The service runs `vcam run -c <absolute config>` and keeps it alive automatically
(restarts after crashes).  Put all camera options — ports, modes, codecs — in the
YAML file.

### Linux notes

The service runs under your **user systemd** session (`systemctl --user`), so it
starts when you log in and stops when you log out.

On headless servers (the EdgeAI Jetson, CI boxes) you usually want it to survive
logout.  Enable lingering once:

```bash
sudo loginctl enable-linger $USER
```

Logs are written to the systemd journal:

```bash
journalctl --user -u vcam.service -f
```

### macOS notes

The LaunchAgent starts at login and is restarted automatically on crash.
Logs land in `~/Library/Logs/vcam-vcam.log` (or `vcam-<name>.log` for a custom
`--name`).

If ffmpeg is installed via Homebrew (i.e., in `/opt/homebrew/bin`) you may need
to install vcam with the same shell so that PATH is captured correctly:

```bash
vcam service install         # run from a shell where `which ffmpeg` returns a path
```

### Distribution notes

The recommended install method is via [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/), both of which produce a standalone vcam executable
that the service unit can reference by absolute path:

```bash
uv tool install .            # installs vcam into its own isolated env
# or
pipx install .

vcam service install
```

A `.deb` package or Homebrew formula is optional and only worth adding if you need
a system-wide `apt install` workflow (e.g., managed fleet deployment).

## Development

```bash
uv sync
uv run pytest
```

CI gates the same three checks, so run them before pushing:

```bash
uv run ruff check .          # lint (pyflakes, import order, pyupgrade, bugbear)
uv run ruff format --check . # formatting, 100 columns
uv run mypy                  # type check src/vcam
```

`ruff check --fix` and `ruff format` apply the mechanical fixes.

## Contributing and project scope

Contributions are welcome, but this repository is intentionally maintained with a narrow scope to keep it sustainable.

- For non-trivial changes, open an issue first and wait for maintainer feedback.
- PRs should be focused, with tests/docs updated when behavior changes.
- Features outside the roadmap may be deferred or declined.
- Inactive issues/PRs may be marked stale and automatically closed.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for full details.

## aarch64 deployment

On aarch64 the CLI downloads the `linux_arm64` MediaMTX build automatically. Prefer a
hardware encoder when transcoding on-device:

```bash
vcam run -c cameras.yaml --mode transcode --encoder h264_nvenc
```

## Docker

Pre-built multi-arch images (amd64 + arm64) are published to Docker Hub as
[`konekuto/vcam`](https://hub.docker.com/r/konekuto/vcam):

```bash
docker pull konekuto/vcam:latest   # or pin a version, e.g. konekuto/vcam:0.1.0
```

The repo also ships a `Dockerfile` (plus a `docker-compose.yml`) so you can build and
test locally without a Python/ffmpeg setup. The image bundles `ffmpeg`, the `vcam` CLI,
and the MediaMTX binary for the image's architecture (pre-downloaded at build time, so
`vcam run` works offline).

Build:

```bash
docker build -t vcam:latest .
```

Serve a config file and a folder of clips:

```bash
docker run --rm -p 8554:8554 \
  -v "$PWD/cameras.yaml:/vcam/cameras.yaml:ro" \
  -v "$PWD/videos:/vcam/videos:ro" \
  vcam:latest run
```

Or use the bundled compose file (expects `./cameras.yaml` and `./videos/`):

```bash
docker compose up -d --build
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
```

Generate throwaway sample clips without a local ffmpeg:

```bash
docker run --rm -v "$PWD/videos:/vcam/videos" \
  --entrypoint /opt/vcam/scripts/make-sample-videos.sh vcam:latest /vcam/videos
```

Run the unit test suite in a container:

```bash
docker build --target test -t vcam-test:latest . && docker run --rm vcam-test:latest
# or: docker compose --profile test run --rm test
```

Multi-arch images (e.g. for aarch64 EdgeAI boxes):

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t vcam:latest --push .
```

Each architecture's image pre-downloads its own MediaMTX binary, so both can run
offline. The default `HEALTHCHECK` probes the MediaMTX API at `127.0.0.1:9997/v3/info`;
if you override `api_port` in your config, adjust or drop it.
