# Configuration file

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
    # sync_group: scene        # opt-in shared scene clock; see synchronized-playback.md

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
      mode: normal             # see "Simulation modes"
```

Command line flags override the file for **every** camera, which is handy for
experiments:

```bash
uv run vcam run -c cameras.yaml --mode transcode --resolution 640x360 --fps 10
```

Other per-camera knobs: `--loop/--no-loop`, `--realtime/--no-realtime` (drop `-re` pacing
to push as fast as possible), `--start-offset` (seek into the file so feeds are de-synced),
`--transport tcp|udp`, `--audio/--no-audio`, and `--encoder` for hardware encoders
(e.g. `--encoder h264_nvenc` on hardware that supports it).

Legacy `streams.yaml` manifests (`streams:` with `offset_seconds`) are accepted too:

```bash
uv run vcam list -c streams.yaml
```

## Live sources and simulator ingest

`source` can also be an `rtsp://` or `udp://` URL, preserved exactly (including
query parameters). Live inputs ignore `loop` and `realtime`: no `-stream_loop`
or `-re` is applied, no file is required, and `start_offset` must be zero.
They cannot join a `sync_group`, which remains file-replay only. Simulator
cross-camera alignment and ground truth use the simulator manifest's timing
metadata, not VCAM's replay clock or RTCP wall-clock timestamps.

```yaml
ingest:                       # optional; omit if an ingest already runs elsewhere
  host: 127.0.0.1              # loopback by default
  rtsp_port: 8654
  password: ""                # dedicated "sim" user; set a password for off-host publishing
cameras:
  - name: anpr-front
    source: rtsp://127.0.0.1:8654/sim/anpr-front
    source_timeout: 5         # live socket read / probe timeout in seconds (0 < value <= 60)
    video:
      resolution: 1920x1080
      fps: 30
  - name: noisy
    source: rtsp://127.0.0.1:8654/sim/overview
    simulation:
      mode: noise
```

Live `auto` selects **copy** without faults and **transcode** with a non-normal
simulation or custom filters. Video settings apply only when transcoding.
RTSP inputs use TCP; both live protocols use `-fflags nobuffer` and a bounded
socket read timeout. Probes are bounded too and run independently so an absent
camera cannot stall other publishers' supervision. Live probe analysis is capped
at 0.5 seconds of media / 1 MB, rather than the longer file-probe defaults.
File source behavior is unchanged.

The optional ingest is a separately supervised MediaMTX instance, TCP-only,
with paths `~^sim/.+$`. Only user `sim` can publish; anonymous reads and API
access are loopback-only. Camera-facing servers and their authentication are
unchanged. Ingest RTSP, API and UDP port allocations cannot overlap the
camera/replay listeners. Generated configs are owner-readable/writable only.
For remote publishing, explicitly change `ingest.host` and set `ingest.password`;
keep its local API and reader restrictions.

The simulator publisher URL must include the username:
`rtsp://sim:@127.0.0.1:8654/sim/<camera-id>` (or `sim:<password>@`).
The credential-free `ingest_url` in the manifest is still readable locally.
An existing anonymous simulator development ingest can also be used: leave
the VCAM `ingest` block out to avoid starting a second listener.

### Importing `sim-streams.json`

```bash
uv run vcam import-sim sim-streams.json                 # YAML to stdout
uv run vcam import-sim sim-streams.json -o cameras.yaml # refuses overwrite without --force
```

The importer requires schema `eais-sim-streams/1`, nonempty unique camera ids,
live `ingest_url`, positive width/height and fps. It maps id to name, URL to
source, width/height to `video.resolution`, fps to `video.fps`, and the optional
`hevc` codec to `video.codec: h265`. It ignores unrelated metadata and does not
probe streams, start ingest, watch for manifest updates, or consume ground-truth
events (including the optional SSE endpoint). Add `ingest: {}` explicitly if
VCAM should own ingest.

### Synthetic verification (no Unity required)

Start VCAM with the example above, then publish a synthetic camera on macOS:

```bash
ffmpeg -re -f lavfi -i testsrc2=size=1920x1080:rate=30 \
  -c:v h264_videotoolbox -profile:v high -pix_fmt yuv420p -g 30 -bf 0 \
  -f rtsp -rtsp_transport tcp rtsp://sim:@127.0.0.1:8654/sim/anpr-front
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/anpr-front
```

On Linux use `libx264 -preset veryfast -tune zerolatency` instead of
`h264_videotoolbox`. The opt-in integration test starts and cleans up its own
stack and publisher (ports 8554/8654 must be free):

```bash
uv run vcam install-server
VCAM_LIVE_TESTS=1 uv run pytest tests/test_live_integration.py -s
```

It compares ingest/output codec, profile, dimensions, frame rate, pixel format,
B-frames and color tags; measures the copy forwarder's CPU over 15 seconds
(must be below 5% of one core); verifies one clean and one faulted camera's
encoder commands; kills/restarts the source and checks waiting/recovery with
`--max-restarts 0`; rejects anonymous/wrong-user ingest publishers; and checks
UDP forwarding and read-timeout recovery. This is a stream-contract test,
not a GPU benchmark.

## How `start_offset` behaves

To align rather than de-sync a set of cameras, assign the same `sync_group` to
at least two enabled cameras. Synchronized groups require compatible H.264
constant-frame-rate assets, matching rate/frame count, looping copy mode over
TCP, and no offset, audio, encoding overrides or simulations. Configuration is
validated again after CLI overrides. See [Synchronized playback](synchronized-playback.md)
for asset admission, recovery, health reporting and examples.

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

## Ports and paths

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
Live-source outages are also exempt from that budget and retry indefinitely,
with 1s to 30s backoff. Health cameras include `state`: `waiting-for-source`,
`starting`, `running`, `failed`, `suspended` or `stopped`. A source that is
reachable but whose publisher repeatedly fails still respects `--max-restarts`.
`Ctrl-C` stops the publishers and then the servers.

## Server binary

Resolution order: `--mediamtx-binary` → `$VCAM_MEDIAMTX_BIN` → `mediamtx` on `PATH` →
local cache → download from GitHub releases (checksum-verified).

```bash
uv run vcam install-server                     # pre-fetch into the cache
uv run vcam run --no-download                  # never reach the network
VCAM_CACHE_DIR=/opt/vcam uv run vcam install-server
```
