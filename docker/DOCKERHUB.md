# vcam — Virtual RTSP Camera Simulator

Turn video files into looping RTSP camera streams for developing and testing video
analytics pipelines — NVIDIA DeepStream, Frigate, or any RTSP-based CCTV/NVR software —
without physical IP cameras.

## Quick start

```bash
docker run --rm -p 8554:8554 \
  -v "$PWD/videos:/vcam/videos:ro" \
  konekuto/vcam run --source /vcam/videos/camera.mp4
```

Or serve a config file with multiple cameras:

```bash
docker run --rm -p 8554:8554 \
  -v "$PWD/cameras.yaml:/vcam/cameras.yaml:ro" \
  -v "$PWD/videos:/vcam/videos:ro" \
  konekuto/vcam run
```

## Key capabilities

- **Multiple cameras, one port** — each camera gets its own RTSP path
  (`rtsp://host:8554/cam1`, `.../cam2`, …), or its own port to simulate physically
  separate devices.
- **Real-time looping playback** — sources loop forever, paced to real time, and
  restart automatically if a publisher dies.
- **PCAP replay** — replay packet captures from real cameras byte for byte when a
  synthetic feed isn't faithful enough.
- **Network-failure testing** — combine with tools like `toxiproxy` to simulate
  dropped connections, latency, and jitter against the simulated feeds.
- **Multi-arch** — published for `linux/amd64` and `linux/arm64`, including NVIDIA
  Jetson / edge devices.

## Tags

- `latest`, `<version>` (e.g. `0.3.0`) — stable releases
- `main` — latest build from the default branch

## Full documentation

Configuration reference, capture-replay setup, Docker Compose examples, and
contributing guide: **[github.com/antowan/virtual-rtsp-camera](https://github.com/antowan/virtual-rtsp-camera)**
