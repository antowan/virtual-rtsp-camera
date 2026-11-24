# Docker

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

To check the **runtime image**, including its installed CLI, FFmpeg and
MediaMTX, run the same non-root, network-isolated smoke test as CI:

```bash
docker run --rm --network none -i --entrypoint bash vcam:latest -s < scripts/smoke-control-container.sh
```

It starts an empty engine, adds a camera, decodes a frame over loopback RTSP,
then stops the streams and checks that the original daemon still answers.

Multi-arch images (e.g. for aarch64 EdgeAI boxes):

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t vcam:latest --push .
```

Each architecture's image pre-downloads its own MediaMTX binary, so both can run
offline. The default `HEALTHCHECK` probes the MediaMTX API at `127.0.0.1:9997/v3/info`;
if you override `api_port` in your config, adjust or drop it.

## Controlling a running container

The image includes the optional control server, but does not enable it by
default. For control **inside** the container, add `control: {enabled: true}` to
`cameras.yaml`; leave its listener on loopback and do not publish a control
port. Use a **writable directory mount** for durable edits. Replacing a file
atomically is not possible through a single-file bind mount, even one marked
`:rw`. Media may stay read-only. Use container paths such as `/vcam/videos/a.mp4`
in camera definitions.

Example with a prepared `./config/cameras.yaml` and `./control-state` directory:

```bash
docker run --name vcam-controlled --rm -d \
  -p 8554:8554 \
  -v "$PWD/config:/vcam/config:rw" \
  -v "$PWD/control-state:/home/vcam/control-state:rw" \
  -v "$PWD/videos:/vcam/videos:ro" \
  -e VCAM_STATE_DIR=/home/vcam/control-state \
  -w /vcam/config \
  --health-cmd='vcam status >/dev/null' \
  vcam:latest run -c /vcam/config/cameras.yaml

docker exec vcam-controlled vcam status
docker exec vcam-controlled vcam add /vcam/videos/a.mp4 --name lobby
```

Make both writable directories accessible to the image's non-root `vcam`
user, and restrict control state to its owner. The separate state mount keeps
the token private and stable across container replacements. `vcam status`
checks the **controller**, unlike the default MediaMTX healthcheck: intentionally
stopping stream orchestration should not mark a controlled container unhealthy.

For a host-side client, publishing a port is not enough while the listener
binds only to container loopback. Configure `control.host: 0.0.0.0`,
`control.allow_remote: true`, and `control.tls_cert` / `control.tls_key` pointing
to read-only mounted certificate files. Add `-p 127.0.0.1:8765:8765` to limit
access to the Docker host. Use HTTPS with a trusted certificate whose names
cover the client-facing address, list any non-loopback client-facing hostname
in `control.allowed_hosts`, and provision the control token separately.
Opening a LAN-facing host port is a separate operator decision. Do not publish
the private MediaMTX API as a substitute.

## aarch64 deployment

On aarch64 the CLI downloads the `linux_arm64` MediaMTX build automatically. Prefer a
hardware encoder when transcoding on-device:

```bash
vcam run -c cameras.yaml --mode transcode --encoder h264_nvenc
```
