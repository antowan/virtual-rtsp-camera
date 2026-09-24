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

Multi-arch images (e.g. for aarch64 EdgeAI boxes):

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t vcam:latest --push .
```

Each architecture's image pre-downloads its own MediaMTX binary, so both can run
offline. The default `HEALTHCHECK` probes the MediaMTX API at `127.0.0.1:9997/v3/info`;
if you override `api_port` in your config, adjust or drop it.

## aarch64 deployment

On aarch64 the CLI downloads the `linux_arm64` MediaMTX build automatically. Prefer a
hardware encoder when transcoding on-device:

```bash
vcam run -c cameras.yaml --mode transcode --encoder h264_nvenc
```
