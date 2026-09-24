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

## How `start_offset` behaves

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
`Ctrl-C` stops the publishers and then the servers.

## Server binary

Resolution order: `--mediamtx-binary` → `$VCAM_MEDIAMTX_BIN` → `mediamtx` on `PATH` →
local cache → download from GitHub releases (checksum-verified).

```bash
uv run vcam install-server                     # pre-fetch into the cache
uv run vcam run --no-download                  # never reach the network
VCAM_CACHE_DIR=/opt/vcam uv run vcam install-server
```
