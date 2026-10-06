# Synchronized scene playback

`vcam run` supports opt-in groups of looping H.264 copy publishers. Each group
has one monotonic scene epoch and one isolated process per camera. Existing
independent FFmpeg cameras and capture replays remain unchanged.

## Configuration and install

```yaml
server:
  rtsp_port: 8554

cameras:
  - name: left
    source: videos/left.mp4
    sync_group: scene
  - name: right
    source: videos/right.mp4
    sync_group: scene
  - name: independent
    source: videos/other.mp4
```

The two scene files must describe corresponding scene windows. Matching timing
does not prove that independently filmed assets depict the same event.

For a Python checkout, install the optional backend and launch the normal CLI:

```bash
uv sync --extra sync
uv run --extra sync vcam run --config cameras.yaml --health-file health.json
```

An installed library can use `CameraSpec(sync_group="scene", ...)`,
`CameraStack` and `Supervisor` with the same behavior. The base Python install
does not import PyAV unless a group is admitted. Install `vcam[sync]` to enable
the backend; missing dependencies produce an explicit error before publishers
start.

The Docker runtime includes the `sync` extra and the packaged backend. Use
the same config, video mounts, ports and `run` entrypoint as independent cameras:

```bash
docker run --rm -p 8554:8554 \
  -v "$PWD/cameras.yaml:/vcam/cameras.yaml:ro" \
  -v "$PWD/videos:/vcam/videos:ro" \
  konekuto/vcam:<new-tag> run --config /vcam/cameras.yaml
```

`<new-tag>` is a future image built from this implementation, not an assertion
that an existing published tag contains it. Controlled local application
integration validates this change. Deployment environments require separate
qualification using an image built from the implementation.

## Supported source profile

Admission runs before any server or publisher is started. Every enabled member
of a group must have:

- One H.264 video track in MP4-style four-byte AVCC format.
- Constant rational frame rate from 1 through 120 fps; integral PTS/DTS on that
  frame grid, one frame per access unit, and one-frame packet duration.
- A complete scene starting at presentation frame zero, with exactly the same
  frame count and frame rate as its peers.
- An IDR at scene start and subsequent independently decodable closed GOPs,
  with at most ten seconds between access points. Admission inspects actual IDR
  NALs and decodes each GOP independently; it does not trust keyframe flags.
- At most 64 MiB compressed video and 108000 frames per source, and at most
  256 MiB compressed video per group. Indexed payloads are retained in the
  supervisor and copied into spawned workers; these bounds are not total RSS
  limits. Full admission decodes the scene and can take time for large assets.

Packet copy preserves encoded picture bytes and native PTS-DTS offsets.
Production publishing does not insert the diagnostic SEI used by the feasibility
harness and does not decode/re-encode pictures during playback.

Synchronized cameras require `loop: true`, `realtime: true`, `mode: auto` or
`copy`, `transport: tcp`, `audio: false`, zero `start_offset`, and default
`video`/`simulation` settings. Unsupported combinations, including global CLI
overrides, are errors rather than silent fallbacks. Non-video tracks are not
published. Camera names in groups must be unique across all ports; at least two
members per group must be enabled.

HEVC, VFR, open GOPs, audio publishing, transcoding, scene offsets and simulated
fault modes are not supported for groups in this version. Independent cameras
still support their existing modes.

## Startup, recovery and health

After the normal MediaMTX servers start, group members prepare their RTSP
outputs. A bounded readiness barrier commits the epoch with one second of
startup lead. The shared decode preroll accommodates the deepest B-frame
reordering among members. Absolute rational deadlines advance continuously
across scene wraps without restarting the RTSP muxer.

A member that falls more than one frame behind skips stale compressed data
and waits for a future admitted IDR. A worker exit or stalled write triggers
isolated replacement with exponential retry backoff from one to thirty seconds.
The replacement retains the original group epoch, starts at a future IDR and
rebases its transport-local timestamp origin. Healthy peers are not restarted.
Readers of the failed member must reconnect; socket/SSRC continuity is not
promised. `--max-restarts` applies per member, just as it does for independent
publishers. Exhausted members remain failed in health until the service restarts.

Workers use separate command/telemetry pipes, not child-shared stop locks.
`Ctrl-C`/SIGTERM stops owned workers with bounded termination escalation and then
stops servers. Cleanup attempts all resources even if one fails. Replacing the
scene files while running is not supported: a worker checks its admitted file
hash before publication; source changes fail explicitly and require restarting
the service to admit the new assets.

`vcam list` and `run --dry-run` display the selected groups. Dry-run describes the
backend without importing PyAV or decoding assets; actual asset admission
happens on `run`. `--health-file` adds group epoch/rate/frame count/preroll and
per-member state, generation, progress, global frame, GOP skips, last error and
restart-budget exhaustion. These are publication diagnostics, not a claim that
an arbitrary downstream decoder is displaying a fresh frame.

Groups isolate publisher failures, not shared MediaMTX/server/host failures.
Their scene epochs restart when the VCAM process restarts. Monotonic scheduling
is independent of NTP wall-clock synchronization and is not an RTCP accuracy
guarantee.

## Validation

```bash
uv run --extra prototype pytest
VCAM_SYNC_INTEGRATION=1 uv run --extra prototype pytest \
  tests/test_synchronized.py -k application_path -s
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The opt-in application tests launch the real `vcam run` subprocess with YAML
config, three synchronized views and an independent FFmpeg camera. Serial
44-second runs exercise ten scene wraps, decoded marker/native-PTS progression,
late joins/reconnects, B-frame worker-exit and suspended-worker watchdog recovery,
peer isolation, health and shutdown. Recovery checks include the rejoined member,
not just the healthy pair. The controlled same-frame decoded receipt spread threshold is
33.333 ms at 30 fps; it is not widened to accommodate failures. Receipt timestamps
are taken as frames emerge from the decoder, before diagnostic marker extraction.
The baseline and publisher-recovery profiles also capture RTP marker timestamps
and RTCP Sender Reports at a loopback TCP proxy, associate sender-report clock
mappings with decoded scene frames, and require the derived scene-start times
to agree across grouped publishers and reader reconnects within one frame.
This is a generated-fixture regression check, not a guarantee of original
acquisition time or RTCP accuracy for arbitrary assets, receivers or deployment
environments.

### Controlled local validation observations

The three application profiles passed serially at 44 seconds each with at least
ten scene wraps. An independent FFmpeg camera ran alongside the group; healthy
members had no replacements or GOP skips, and shutdown completed normally.

| Profile | Common healthy frames | Maximum healthy spread | Maximum recovered-member spread |
| --- | --- | --- | --- |
| Baseline, no B frames | 1318 | 22.636 ms | Not applicable |
| Worker exit, B frames | 1314 | 1.980 ms | 2.394 ms |
| Worker suspension, B frames | 1314 | 2.756 ms | 2.756 ms |

Late/reconnected readers also passed the same gate. A repeated baseline passed
with 1317 common frames and 5.084 ms maximum healthy spread. Observed decoded
reader gaps were 3.196 seconds for exit and 6.076 seconds for suspension; these
include reader reconnect, health polling and GOP acquisition, not just publisher
replacement time.

An earlier baseline recorded 41.467 ms using timestamps taken after marker
extraction. The test was corrected to capture decoded receipt before that work,
matching the original diagnostic harness; the corrected profiles and repeat
passed without relaxing the gate. The earlier outlier is not conclusively
attributed to marker work or a publisher defect.

The Linux/arm64 Docker runtime was also built and exercised locally through its
normal entrypoint with read-only fixtures and no external network. Two grouped
publishers and an independent camera each decoded 180 consecutive native-PTS
frames, followed by clean SIGTERM shutdown. This verifies packaging and runtime
startup, not synchronization accuracy in arbitrary deployment environments.

These controlled local measurements are not a universal maximum-skew SLA,
transport-to-frame timestamp certification, or a guarantee for arbitrary
resolutions, receivers, networks and machine loads. Long-running RTP wrap,
representative deployment loads, shared server/host failures and deployment
environment validation remain separate qualification work.
