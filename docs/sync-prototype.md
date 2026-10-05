# Shared-clock playback prototype

This is a **standalone feasibility experiment**, not a new `vcam run` mode or a
production synchronization guarantee. It publishes three generated H.264 views
through an owned, loopback-only MediaMTX instance and checks their decoded scene
markers across loops, late joins, reconnects, and isolated publisher failures.

The question is whether separate copy-mode publishers can share one monotonic
scene clock while a failed member rejoins the current scene without restarting
healthy peers. A single FFmpeg process is not assumed to provide this contract.

## Run locally

From a checkout, with Python >=3.10:

```bash
uv sync --extra prototype
uv run vcam install-server

uv run --extra prototype python -m scripts.sync_prototype \
  --report .sync-prototype/baseline.json
uv run --extra prototype python -m scripts.sync_prototype \
  --b-frames 2 --report .sync-prototype/b-frames.json
uv run --extra prototype python -m scripts.sync_prototype \
  --scenario exit --b-frames 2 --report .sync-prototype/exit.json
uv run --extra prototype python -m scripts.sync_prototype \
  --scenario stall --report .sync-prototype/stall.json
uv run --extra prototype python -m scripts.sync_prototype \
  --scenario backpressure --report .sync-prototype/backpressure.json
```

The default run lasts 44 seconds plus fixture generation and teardown, covering
at least ten scene wraps on healthy observers. `--duration` accepts 28-120 seconds;
short runs do not establish ten-wrap coverage. `--b-frames` accepts 0 or 2.
An outer process enforces a deadline of the requested duration plus 45 seconds.
The experiment creates a dedicated POSIX process group; the outer runner stops
only that owned group if the coordinator or cleanup hangs and preserves a failed
deadline report.

MediaMTX must already be available via the repository's usual binary resolution
order; the runner does not download automatically. `vcam install-server` uses the
existing checksum-verified installer. Use `--mediamtx-binary /path/to/mediamtx`
to select an existing binary explicitly.

The `prototype` extra is optional. Neither the normal CLI nor the runtime Docker
image installs PyAV or NumPy for this experiment. A missing extra produces an
explicit installation error.

All listeners use newly allocated loopback TCP ports. No existing server, source
video, system clock, or deployment is changed. Fixture media and server files live
in a temporary directory removed after the experiment. The runner stops only its
own server, workers, observers, and proxies. The bounded runner requires POSIX
process groups; `stall` requires `SIGSTOP`, and resource sampling uses `resource`.

Reports are deliberately ignored under `.sync-prototype/`. Other output paths are
allowed, but existing reports are never overwritten. Raw diagnostic errors may
contain local filesystem paths; review reports before sharing them.

## What the prototype implements

The fixtures are three distinct 192x96 views, 120 frames each, at 30/1 fps.
Pixel blocks encode a view ID and zero-based scene frame, with inverse blocks to
detect unreadable markers. Deterministic noise outside the marker makes the
backpressure test exercise nontrivial compressed traffic. Each four-second scene
uses closed GOPs with an access point every 30 frames. The B-frame profile forces
reordering rather than allowing the encoder to decide not to use B frames.

The copied H.264 access units additionally carry diagnostic
`user_data_unregistered` SEI with a fixed UUID, view ID, publisher generation,
and unique global frame number. The encoded picture/slice bytes are not
re-encoded; only this small metadata NAL is inserted, using the generated
fixtures' verified four-byte AVCC lengths. The global identity advances across
scene wraps and survives decoder reordering. A reader verifies the SEI identity
against the decoded pixel marker, native PTS progression and common schedule.
Missing, duplicate or conflicting identities fail timestamped-frame validation.
This is an instrumented copy profile, not proof for an arbitrary unchanged
bitstream or a receiver that strips SEI.

Fixture generation verifies every decoded marker and native presentation
timestamp. Packet indexing checks complete frame coverage, monotonic DTS and the
expected access-point schedule. Each generated access point is tested by seeking
and decoding independently. **These checks certify only these generated fixtures**:
arbitrary H.264 keyframe flags are not sufficient proof of closed-GOP/IDR behavior.
The runner intentionally accepts no external source files.

Each isolated publisher prepares its RTSP output before an initial readiness
barrier commits the common monotonic epoch. Packets are dispatched in decode order
against absolute deadlines with a shared three-frame preroll lead. PTS-DTS offsets
are preserved. Timestamp calculations use rational arithmetic.

The compressed fixture is indexed in memory, bounded to 16 MiB per worker.
File looping retains the same RTSP muxer and publication: both PTS and DTS advance
by exactly 120 native frames at each scene wrap. There is no per-loop process
restart. B-frame RTP timestamps may reorder on the wire; that is not a rewind of
the presentation timeline.

When a worker is late by more than one frame, it abandons stale compressed data
and schedules a future verified access point, rather than dropping arbitrary
P/B packets or draining a stale backlog. A lost worker starts a new publication,
rebases its muxer-local timestamps to its rejoin point, and retains the group's
global scene epoch. Transport continuity through process/socket death is not
promised; affected readers reconnect.

The coordinator watches actual completed writes. One second without progress,
after a three-second startup/rejoin grace, triggers replacement of that worker
only. Output writes use PyAV's interrupt timeout; the process watchdog remains
necessary because native buffering and blocking writes are not an exact freshness
signal. Recovery attempts are bounded.

Child telemetry uses bounded, authenticated loopback datagrams with registered
producer IDs, sequence numbers and a terminal record. Detected gaps or a missing
terminal record fail the report, including missing trailing telemetry when no
later data frame arrives. Evidence is drained after child shutdown before
validation. A forcibly replaced injected publisher can have explicitly recorded
incomplete telemetry during its recovery window; it is not claimed complete.
Suspending a child must not hold
a shared telemetry lock or block the coordinator. Cleanup resumes an owned
suspended child before requesting termination. This isolation is tested directly.
Cleanup attempts every owned resource even if another cleanup fails, retaining
each failure in the report; the outer deadline is the backstop for blocking calls.

## Observation and fault scenarios

Three independent main readers decode markers. An additional reader joins at
five seconds, disconnects at nine seconds, and is replaced by a fresh reader.
Controlled readers use a small probe buffer because ordinary stream probing can
queue startup video before returning decoded frames. Up to two initial decoded
frames without PTS are explicitly recorded as decoder preroll, never claimed as
timestamp-verified samples; missing PTS after that fails.

**Global frame identity is decoded from SEI, never inferred by choosing the loop
nearest wall time.** Pixel marker and PTS progression must agree with that
identity. Both main and additional readers must deliver consecutive eligible
scene frames, at least one second of progress, and fresh output through the end
of their observation window (nine seconds for the deliberately disconnected
late reader). Late/reconnected readers are compared frame-for-frame with the
established view-0 reader under the same one-frame receipt-spread threshold.
Missing/duplicate frames, insufficient overlap, stale content, backward
presentation timestamps or a whole-scene lag fail the check.

Loopback reader proxies independently retain RTP access-unit timestamps/SSRCs and
RTCP Sender Reports. These are diagnostic records, not an assertion that SR
wall-clock correctness establishes scene alignment. Raw RTP timestamps must not
be compared directly between SSRCs.

The fault cases affect only view 1:

| Scenario | Injection | Required evidence |
| --- | --- | --- |
| `baseline` | None | Normal looping, marker/PTS agreement, late live joins |
| `exit` | Stop the owned worker at 12 s | New publication rejoins current scene; peers do not restart |
| `stall` | Suspend the owned worker at 12 s | Progress watchdog replaces it; peers continue |
| `backpressure` | Stop reading/forwarding publisher traffic from 12-22 s | Bounded proxy buffers fill, progress watchdog fires, publisher recovers after forwarding resumes |

The publishing proxy has a 64 KiB cap per direction. Socket buffers can still
accept traffic before blocking: the report records when the watchdog actually
fires. Merely pausing a reader would not establish publisher backpressure.

An injected member has an explicit publication-generation recovery window.
Outage samples remain in the report but are ineligible for healthy three-view
skew calculations until a fresh reconnected session publishes the current
generation. Recovery is confirmed only after 30 consecutive decoded current
frames, not a single frame, running PID or successful `mux()` call. The reported
outage ends at the first frame of that subsequently confirmed sequence; its
confirmation time is separately recorded. Once confirmed, the recovered member
must remain fresh through the end, and a later unplanned publisher/reader failure
fails the run. Expected disconnects are scoped to old reader sessions, not
permanently exempted by camera name.
The healthy-peer pair (views 0 and 2) is independently checked against the same
one-frame maximum throughout the run, including the other member's outage.

Healthy peers must retain their initial worker generation, avoid skipping GOPs,
and continue changing frames. An unexpected observer or worker failure is an
error; injected-member disconnects are retained as expected fault diagnostics.

## Pass/fail and limitations

Exit status is zero only for `passed_controlled_profile`. Assertion failures,
unexpected backend errors, cleanup failures, incomplete healthy telemetry and
outer deadline expiry produce a failed report and nonzero status. Disconnect
errors of the injected member inside its recovery window are expected
diagnostics; later errors after confirmed recovery are not exempt.

The healthy controlled-reader gate uses **maximum**, not p99, decoded receipt
spread for the same global scene frame: at most 33.333 ms at 30 fps. The report
also includes overlap count, duplicates, healthy frame gaps, scene wraps,
publication-write lateness, decoded content age, recovery events, backend
versions, and per-worker CPU/peak RSS samples. Decoded age exceeding 500 ms is a
separate stale-content failure, not permission to claim that absolute latency is
within one frame.
Healthy observers, including the recovered member, must also remain active
through the end of the measurement.
RSS samples use native `getrusage` units (bytes on macOS, KiB on Linux).

This is an observed result for one deliberately bounded profile, **not a
certification or maximum-skew SLA**:

- Measurements include observer scheduling, decoding and proxy latency; there is
  no independently certified <=5 ms measurement-uncertainty budget yet.
- Pixel markers alone are cyclic. Added in-band global identities remove the
  initial whole-period ambiguity in this instrumented copy profile; they do not
  independently certify a real asset's multi-view scene provenance.
- SRs are recorded but not automatically mapped to every decoded frame. No RTCP
  clock-accuracy guarantee or realtime clock adjustment is implemented.
- Healthy measurements do not include intentional outage windows. Copy recovery
  includes a future-access-point wait, output setup and reader reconnect latency.
- This is small synthetic video on the local host, not a resolution/load,
  architecture, hardware-encoder, or arbitrary-network certification.
- Transcoding, audio, HEVC, variable frame rate, unequal scene windows, user
  offsets, simulations, persistent epoch across host restart, and production
  configuration/API integration are not implemented.
- The MediaMTX server and coordinator remain shared infrastructure. Isolated
  camera workers do not isolate a server/host failure.

The next implementation decision depends on these measurements and explicit
remaining gates, not on the presence of a passing timing-helper unit test.

## Example observations

Serial 44-second local experiments using PyAV 16.1.0 (libavformat 62.3.100,
libavcodec 62.11.100), MediaMTX 1.20.1 and a macOS x86_64 Python process produced
the following observations. All used the generated 192x96, 30 fps profile above.
They are reproducible commands, not portable acceptance guarantees.

| Scenario | B frames | Maximum healthy receipt spread | Observed member outage |
| --- | --- | --- | --- |
| Normal looping | 2 | 11.15 ms | None |
| Worker exit | 2 | 19.83 ms | 1.17 s |
| Worker suspension | 0 | 3.68 ms | 2.17 s |
| Ten-second publishing backpressure | 0 | 2.93 ms | 11.17 s |

Healthy peers decoded at least ten scene wraps without GOP skipping or worker
replacement. Their sampled worker peak RSS was approximately 49-50 MiB, with
roughly 0.9 CPU seconds per worker over the run. These figures exclude the
coordinator, observers, fixture generation, proxies and MediaMTX; they do not
estimate production-resolution requirements. Backpressure caused two watchdog
replacements of the affected member before recovery.

Earlier experiments also **failed**: ordinary reader probing accumulated startup
video, concurrent load caused healthy publishers to miss deadlines, and suspending
a shared multiprocessing-queue producer blocked the coordinator. The harness now
uses controlled reader probing and isolated datagram telemetry. Deadline misses
still fail the controlled profile rather than being hidden by a percentile or
silently widening the one-frame threshold. Run timing experiments serially and
retain both passing and failing reports when evaluating another environment.
Injected-member reader disconnects can print backend tracebacks; they are retained
as expected fault diagnostics, not claimed as uninterrupted service.

An adversarial review of the first validator also reproduced false passes for
late readers 800 ms behind and a recovered reader ending 14 seconds early.
Those cases now fail regression tests. The revised schema-version-2 reports
require sustained/final reader health, validate late-reader skew, include unique
decoded loop identity and account for terminal telemetry. Earlier timing reports
are not evidence of passing these strengthened gates.

## Design decision and remaining gates

Separate native FFmpeg commands preserve failure isolation but do not provide a
shared startup/rejoin scene epoch by themselves. A single multi-output FFmpeg
process is a useful baseline, but shares a process failure domain and can couple
outputs under backpressure; it is not the chosen isolation boundary. This
prototype instead keeps one persistent libavformat output per isolated worker,
sharing only the scene epoch and scheduling contract.

A production backend would need asset admission (scene windows, rational rates,
verified access points and offset policy), robust bounded control/telemetry,
versioned group configuration and health reporting, and an explicit encode-mode
decision. Decode/re-encode could provide dynamic visible overlays and more
flexible recovery points, at higher CPU/GPU cost; it is not implemented here.
Existing independent cameras should remain the default.

Before promoting the approach, correlate decoded frames with independently
measured transport timing, establish a
measurement-uncertainty budget, and test representative resolutions, load,
networks and host architectures. Also exercise the 32-bit RTP timestamp wrap
(about 13.3 hours at 90 kHz), long-running reconnect/recovery, and server/host
failures. The rational-clock day-long unit test is not evidence that RTP wrap or
long-running media behavior has been exercised.

## Tests

Deterministic timing/proxy tests work without the prototype extra; fixture tests
skip explicitly when PyAV/NumPy is unavailable:

```bash
uv run --extra prototype pytest tests/test_sync_prototype.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run mypy --explicit-package-bases scripts/sync_prototype
```

The integration matrix is opt-in, runs serially and owns all of its resources:

```bash
VCAM_SYNC_INTEGRATION=1 uv run --extra prototype pytest \
  tests/test_sync_prototype.py -k local_rtsp_prototype
```

It covers baseline without/with B frames, exit with B frames, suspension, and
publishing backpressure. Each case runs inside the externally bounded runner;
the timeout is not merely an elapsed-time assertion after a potentially hung
function returns. Unit tests also exercise whole-scene lag, stale joins,
post-recovery failures, missing terminal telemetry and cleanup errors.

The existing `vcam run` commands, camera schema, independent FFmpeg command
builder, and capture replay semantics are unchanged.
