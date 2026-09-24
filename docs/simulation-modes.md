# Simulation modes

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
