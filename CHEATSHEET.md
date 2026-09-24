# vcam cheat sheet

One-page map of what `vcam` is for and the commands that get you there.
Full details live in [README.md](README.md) and the [docs/](docs/) guides.

## The mental model

```mermaid
flowchart LR
    V["video files<br/>videos/*.mp4"] --> C["cameras.yaml<br/>(or CLI flags)"]
    C --> R["vcam run"]
    R --> F["ffmpeg publisher<br/>per camera"]
    F --> M["MediaMTX<br/>RTSP server"]
    M --> U["rtsp://host:8554/cam1<br/>rtsp://host:8554/cam2"]
    U --> D["DeepStream / Node-RED /<br/>VLC / ffprobe"]
    P["capture<br/>camera.pcap"] --> RP["vcam replay<br/>(own RTSP server)"]
    RP --> U2["rtsp://host:8600/cam1"]
    U2 --> D
```

One server process per **port**, one path per **camera**, one ffmpeg publisher per camera,
restarted automatically if it dies. Replays sit beside that pipeline, not inside it: they
own their listener so no component can re-packetise the captured RTP.

## Command map

| Command | What it's for |
| --- | --- |
| `vcam doctor` | Check ffmpeg / ffprobe / MediaMTX before anything else |
| `vcam probe FILE` | Inspect a clip; shows which mode `auto` would choose |
| `vcam replay CAPTURE` | Serve a `.pcap`/`.pcapng` of a real camera, byte for byte (`--list-tracks` to inspect) |
| `vcam generate [-d dir]` | **Interactive wizard** — scan a folder, answer prompts, write `cameras.yaml` |
| `vcam init [-s file …]` | Create `./cameras.yaml` from a template |
| `vcam add FILE -n cam2` | Append a camera to the config |
| `vcam show` | Print the resolved config (after defaults + overrides) |
| `vcam list` | Table of cameras + their URLs |
| `vcam urls` | One URL per line — pipe into scripts |
| `vcam run` | Start the servers and publishers (Ctrl-C stops) |
| `vcam install-server` | Pre-fetch the MediaMTX binary into the cache |
| `vcam clock-status` | Show clock state; measure NTP offset (read-only) |
| `vcam service …` | Install / start / stop / status / logs for the background service |

## Use case 1 — one clip, one camera, right now

```bash
uv run vcam run --source videos/cam1.mp4
# -> rtsp://127.0.0.1:8554/cam1
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
```

## Use case 2 — several cameras on one port

```bash
uv run vcam run \
  --camera cam1=videos/cam1.mp4 \
  --camera cam2=videos/cam2.mp4 \
  --camera cam3=videos/cam3.mp4
```

Beyond two or three cameras, switch to a config file.
The fastest way is the interactive wizard:

```bash
uv run vcam generate -d videos/  # scan the videos/ folder, answer prompts
uv run vcam run                  # picks up ./cameras.yaml automatically
```

Or build it incrementally:

```bash
uv run vcam init -s videos/cam1.mp4 -s videos/cam2.mp4
uv run vcam add videos/cam3.mp4 -n entrance
uv run vcam run          # picks up ./cameras.yaml automatically
```

## Use case 3 — de-sync feeds fed from the same clip

Give each camera a `start_offset` so they aren't showing identical frames.

```yaml
cameras:
  - { name: cam1, source: videos/loop.mp4, start_offset: 0 }
  - { name: cam2, source: videos/loop.mp4, start_offset: 12 }
  - { name: cam3, source: videos/loop.mp4, start_offset: 24, mode: transcode }
```

Caveat: in `copy` mode the seek snaps to the nearest keyframe (~10 s steps at the default
GOP). Use `mode: transcode` for frame-accurate offsets, or re-encode with a short GOP.

## Use case 4 — control the pixels on the wire

| `--mode` | Behaviour | Use when |
| --- | --- | --- |
| `auto` *(default)* | copy if already H.264/HEVC, else transcode | you don't care |
| `copy` | pure passthrough, near-zero CPU | many cameras on a small box |
| `transcode` | apply resolution / fps / bitrate / codec / GOP | testing a specific profile |

```bash
# cheapest possible: many streams, no re-encode
uv run vcam run -s videos/cam1.mp4 --mode copy

# force a 720p15 2 Mbit/s H.264 feed whatever the source is
uv run vcam run -s videos/cam1.mp4 \
  --mode transcode --resolution 1280x720 --fps 15 --bitrate 2M --gop 30

# hardware encode on Jetson / NVIDIA
uv run vcam run -c cameras.yaml --mode transcode --encoder h264_nvenc
```

## Use case 5 — simulate a misbehaving camera

`--simulation` (or `simulation:` per camera) injects faults so you can test how your
pipeline reacts to bad hardware.

| Mode | What the reader sees |
| --- | --- |
| `normal` | untouched feed |
| `noise` | grainy image (`--noise-level 1..100`) |
| `degraded` | starved bitrate (150k) + long GOP (300) — blocky, smeary video |
| `frozen` | a stuck frame |
| `blackout` | black image |
| `flaky` | the stream drops and comes back, on a schedule |
| `stutter` | recurring freezes / hiccups |

```bash
uv run vcam run -s videos/cam1.mp4 --simulation noise --noise-level 40
uv run vcam run -s videos/cam1.mp4 --simulation flaky \
  --simulation-interval 30 --simulation-duration 5
```

`flaky` and `stutter` are driven by the supervisor at run time; the others are baked into
the encoder filter chain. Extra ffmpeg filters: `--simulation-filters 'gblur=sigma=2'`.

## Use case 6 — replay a real camera's packets

When a synthetic fault is not faithful enough, capture the real camera and replay the RTP
byte for byte. No MediaMTX, no ffmpeg, no re-packetisation — same payloads, same SSRC,
same sequence numbers, same fragmentation, same timing.

```bash
# capture (UDP or TCP-interleaved, both work)
sudo tcpdump -i eth0 -s 0 -w camera.pcap host 192.168.1.64 and tcp port 554

uv sync --extra replay                              # scapy is an optional extra
uv run vcam replay camera.pcap --list-tracks        # what's in it?
uv run vcam replay camera.pcap --port 8600 --path cam1
# -> rtsp://127.0.0.1:8600/cam1
```

| Flag | Why |
| --- | --- |
| `--port` | replays own their listener — pick a port no camera uses |
| `--sdp file` | capture has no `DESCRIBE`; supply the session description |
| `--no-loop` | play once and stop |
| `--no-rewrite-on-loop` | let the rewind be visible (decoders stall — that's the point) |
| `--speed 2.0` | replay faster than captured |
| `--username/--password` | require basic auth (`$VCAM_REPLAY_PASSWORD` keeps the secret out of argv) |

Not supported by design: `PAUSE` (answered `501` — resuming would rewind the RTP counters),
seeking, and RTCP Sender Reports. Captures over 256 MB are refused (loading costs ~6× the
file size in RAM); trim them or raise `$VCAM_MAX_CAPTURE_BYTES`.

In YAML (`source` is the capture, one port each):

```yaml
replays:
  - { name: broken-cam, source: captures/broken.pcap, port: 8600 }
```

⚠️ Captures record RTSP `Authorization` headers and `rtsp://user:pass@host` URIs in clear
text — `--list-tracks` warns you. Scrub before sharing: `editcap -E 0 in.pcap out.pcap`.

## Use case 7 — emulate physically separate devices

Default is one port, many paths. Give a camera its own `port:` only when you need a
separate device (a second MediaMTX instance is spawned per port group).

```yaml
cameras:
  - { name: cam1, source: videos/cam1.mp4 }            # :8554/cam1
  - { name: cam3, source: videos/cam3.mp4, port: 8555 } # :8555/cam3
```

## Use case 8 — require credentials

```bash
uv run vcam run -s videos/cam1.mp4 --username reader --password s3cret
# -> ******127.0.0.1:8554/cam1
```

Readers must authenticate; local publishers keep working without a password, and anonymous
publishing is restricted to loopback. Without `auth`, everything is anonymous.

## Use case 9 — run it in Docker (no local Python/ffmpeg)

```bash
docker build -t vcam:latest .

docker run --rm -p 8554:8554 \
  -v "$PWD/cameras.yaml:/vcam/cameras.yaml:ro" \
  -v "$PWD/videos:/vcam/videos:ro" \
  vcam:latest run

# or
docker compose up -d --build
```

Sample clips without a local ffmpeg:

```bash
docker run --rm -v "$PWD/videos:/vcam/videos" \
  --entrypoint /opt/vcam/scripts/make-sample-videos.sh vcam:latest /vcam/videos
```

Multi-arch (aarch64 EdgeAI boxes):

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t vcam:latest --push .
```

## Use case 10 — operate / debug a running stack

```bash
uv run vcam run --dry-run                            # print the plan, start nothing
uv run vcam run -v                                   # debug logging
uv run vcam run --health-file /tmp/vcam-health.json  # JSON snapshot every 5s
uv run vcam run --work-dir .vcam                     # keep generated mediamtx.yml
uv run vcam run --max-restarts 5                     # cap publisher restarts
uv run vcam run --no-download                        # never touch the network
```

Read a stream back:

```bash
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
ffplay  -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
```

## Flags you reach for most

| Flag | Effect |
| --- | --- |
| `-c, --config` | YAML file (default `./cameras.yaml`, `.yml`, or `vcam.yaml`/`.yml`) |
| `-s, --source` / `-n, --name` | single camera from one file |
| `--camera name=file` | repeatable, multiple cameras inline |
| `--host` / `--port` | bind address / shared RTSP port (default `8554`) |
| `--mode`, `--resolution`, `--fps`, `--bitrate`, `--codec`, `--encoder`, `--gop` | video shaping |
| `--loop/--no-loop`, `--realtime/--no-realtime` | looping and real-time pacing |
| `--start-offset` | seek N seconds in, to de-sync |
| `--transport tcp\|udp`, `--audio/--no-audio` | publishing details |
| `--simulation`, `--noise-level`, `--simulation-interval/-duration` | fault injection |
| `-u/--username`, `-P/--password` | reader auth |
| `--ntp-server` | sync the clock before starting (Docker only) |

CLI flags override the config file for **every** camera — handy for quick experiments:

```bash
uv run vcam run -c cameras.yaml --mode transcode --resolution 640x360 --fps 10
```

## Gotchas

- `start_offset` applies **once at startup**; later loops replay from the file start, so the
  phase shift is permanent (that's the point).
- In `copy` mode seeks snap to keyframes — use `transcode` for precision.
- `api_port` (default `9997`) is loopback-only; the Docker `HEALTHCHECK` probes it, so
  adjust or drop the healthcheck if you change it.
- MediaMTX resolution order: `--mediamtx-binary` → `$VCAM_MEDIAMTX_BIN` → `PATH` → cache →
  download. `vcam doctor` tells you which one you'd get.
- A replay **cannot** share a camera's port — it runs its own RTSP listener instead of
  MediaMTX, and `vcam` refuses a config where the ports collide.
- Looping a capture rewrites RTP sequence numbers and timestamps forward so decoders don't
  stall; the packet payloads stay byte-identical. `--no-rewrite-on-loop` opts out.
- A stack with only `replays:` and no `cameras:` starts no MediaMTX at all.
