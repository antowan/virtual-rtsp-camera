---
goal: RTSP traffic capture (proxy mode) and PCAP replay for virtual camera
version: 1.1
date_created: 2026-08-26
date_updated: 2026-08-26
owner: antoine-em
status: 'Replay complete (Phases 1, 2, 4); proxy deferred (Phase 3)'
tags: [feature, architecture, networking, debugging]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

This plan adds two complementary capabilities to `vcam`:

1. **Proxy mode** — vcam sits between a real IP camera and the EdgeAI Station. It transparently forwards RTSP traffic while capturing the raw RTP/RTSP packets into a PCAP file (equivalent to a Wireshark capture). Useful to record real-world traffic from production cameras for later reproduction. **Deferred to a follow-up revision** (see §9).

2. **PCAP replay mode** — vcam reads a previously recorded PCAP file (`.pcap` / `.pcapng`) and replays the original RTP packets over a fresh RTSP session toward any consumer (e.g., DeepStream on the EdgeAI Station). The goal is **exact wire-level reproduction**: same codec, same packetisation, same packet timing — enabling reproduction of subtle pipeline errors that a re-encoded video file would never trigger.

## Revision 1.1 — what changed and why

Revision 1.0 was reviewed before implementation and four load-bearing assumptions did not survive:

- **`source: rtp://` does not exist.** MediaMTX v1.20.1 (the version pinned in `binaries.py`) spells it `udp+rtp://ip:port` and additionally requires a `rtpSDP:` path field. Path B1 as written would not have started.
- **MediaMTX cannot be in the replay path at all.** MediaMTX depacketises RTP into media units and re-packetises per reader: new SSRC, new sequence numbers, new RTP timestamps, its own FU-A fragmentation. Every packet-level artefact the feature exists to reproduce — loss, reordering, fragmentation quirks, timestamp jumps, in-band parameter-set changes — is sanitised away. REQ-003 and "publish via MediaMTX" are mutually exclusive. **Replay therefore serves its own minimal RTSP server** (ALT-005).
- **RTP is usually not on UDP/5004.** RTP ports are negotiated per-session in `SETUP`, and most real IP cameras negotiate `RTP/AVP/TCP`, which carries RTP `$`-framed *inside the RTSP TCP connection*. TASK-014's "filter UDP packets on port 5004" would find nothing in a typical capture. Both transports must be supported on the read side.
- **Naive looping stalls the decoder.** Rewinding to the first packet replays the original sequence numbers and RTP timestamps, so the receiver sees a large backwards discontinuity. Loop replay must rewrite seq/timestamp per SSRC to stay monotonic.

Two dependencies were also dropped: `libpcap-dev` existed only to support a C binding that is not used (scapy is pure Python; `tcpdump` ships its own libpcap runtime), and removing it preserves the deliberately minimal CVE surface of the current Dockerfile.

---

## 1. Requirements & Constraints

- **REQ-001**: `vcam proxy` sub-command must accept an upstream RTSP URL (real camera) and expose a local RTSP path; all readers receive a live tee of the upstream stream.
- **REQ-002**: While proxying, every RTP/RTCP/RTSP packet (including RTSP handshake messages) must be saved verbatim into a PCAP file with original timestamps.
- **REQ-003**: `vcam replay` sub-command must accept a PCAP file path and serve it as an RTSP stream; the RTP packets must be delivered **byte-for-byte as captured** (original SSRC, sequence numbers, RTP timestamps, marker bits and fragmentation) and packet inter-arrival timing must be preserved (±2 ms tolerance at replay).
- **REQ-004**: Both sub-commands must integrate with the existing `cameras.yaml` configuration format; replay entries live under a top-level `replays:` list, keeping `cameras:` semantics untouched.
- **REQ-005**: The proxy PCAP capture must survive the upstream camera going offline and resuming (capture file is flushed and kept valid at all times).
- **REQ-006**: Replay must support looping the PCAP (same flag semantics as the existing video loop behaviour).
- **REQ-007**: The existing `run` sub-command and all current behaviour must be fully preserved; this plan introduces additive functionality only.
- **REQ-008**: Replay must read RTP from both transports a real capture can contain: plain UDP flows, and `$`-framed interleaved data inside the RTSP TCP connection (`RTP/AVP/TCP`). RTP endpoints must be discovered from the captured `SETUP` responses, never hard-coded.
- **REQ-009**: When looping, per-SSRC sequence numbers and RTP timestamps must be rewritten so the stream stays monotonic across the loop boundary; the RTP payload itself must remain untouched.
- **REQ-010**: Replay must serve readers over both `RTP/AVP` (UDP) and `RTP/AVP/TCP` (interleaved), because DeepStream deployments use either.
- **SEC-001**: PCAP files may contain credentials embedded in RTSP `DESCRIBE` / `ANNOUNCE` headers; the tool must log a warning when writing **or replaying** such a capture and must document redaction procedures.
- **CON-001**: Must run on `linux/amd64` and `linux/arm64`. macOS support for development is required but performance is best-effort.
- **CON-002**: No new mandatory system-level dependencies may be added to the Docker image beyond what ships in the base image (`python:3.12-slim`). PCAP file I/O must therefore be pure Python.
- **CON-003**: PCAP replay must not require a running `ffmpeg` process **and must not route through MediaMTX** — both re-packetise and would destroy the fidelity REQ-003 exists to guarantee. Replay owns its own RTSP listener.
- **CON-004**: `scapy` must not be imported on the CLI hot path; it costs ~12 MB and a noticeable import delay, so it is loaded lazily inside the PCAP module only when a capture is actually touched.
- **GUD-001**: Follow the existing module layout (`src/vcam/`); new source files must each have a single clear responsibility.
- **GUD-002**: All new CLI parameters must follow the existing Typer conventions in `cli.py`; long-form options only, with short aliases where existing patterns do so.
- **GUD-003**: New code must achieve ≥ 90 % line coverage via `pytest`; mocks and synthetic PCAP fixtures must substitute real network I/O in unit tests.
- **PAT-001**: Use the existing `Supervisor` / `ManagedProcess` pattern for managing replay servers declared in `cameras.yaml` — the supervisor spawns `vcam replay` as a child process exactly like it spawns ffmpeg publishers.

---

## 2. Implementation Steps

### Phase 1 — Research & Feasibility Spike

- GOAL-001: Confirm technical approach for each sub-feature; document findings; decide on Python libraries.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | **Evaluate packet-capture libraries.** Compared `pyshark`, `scapy` and `python-libpcap`. **Decision: `scapy`** — pure Python, no C toolchain on aarch64, reads/writes both `.pcap` and `.pcapng`, and handles link-layer decoding (`EN10MB`, `LINUX_SLL`/`SLL2` from `tcpdump -i any`, `RAW`, `NULL` from macOS `lo0`) which a hand-rolled reader would have to reimplement. Imported lazily per CON-004. | ✅ | 2026-08-26 |
| TASK-002 | **Evaluate RTP injection approaches.** Compared ffmpeg re-mux, MediaMTX `udp+rtp://` + `rtpSDP`, and a self-hosted RTSP listener. **Decision: self-hosted RTSP listener.** Both other options re-packetise, which is fatal for REQ-003 (see ALT-005). | ✅ | 2026-08-26 |
| TASK-003 | **Prototype proxy interception.** Deferred with the proxy feature. Note for the follow-up: the revision 1.0 sketch (byte-pipe camera ↔ MediaMTX `ANNOUNCE`) cannot work — RTSP is not a symmetric byte pipe, and the two ends play opposite roles. A real transparent proxy is an RTSP *server* to downstream readers and an RTSP *client* to the camera, rewriting request URIs and `Transport` headers; MediaMTX is not in that path. | ⏸ | |
| TASK-004 | **Confirm PCAP replay with real DeepStream pipeline.** Superseded: replay no longer depends on `tcpreplay` semantics (which rewrites nothing and requires matching L2/L3 addressing). Validation is now TASK-021, an end-to-end run of `vcam replay` against DeepStream. | ➖ | |

### Phase 2 — Core Infrastructure

- GOAL-002: Lay the shared building blocks required by replay (and reusable by the deferred proxy).

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | **Add `pcap.py` module** at `src/vcam/pcap.py`. Lazy-import scapy. Expose `iter_datagrams(path) -> Iterator[Datagram]` yielding `(ts, proto, src, dst, payload, tcp_seq)` with link-layer, VLAN, IPv4 and IPv6 decoding. Keep `PcapWriter` for the proxy and for test fixtures, with `write_udp()` / `write_tcp()` helpers that synthesise valid frames instead of the revision-1.0 `Ether(raw_chunk)` which produced corrupt captures. | ✅ | 2026-08-26 |
| TASK-006 | **Add `rtp.py` module** at `src/vcam/rtp.py`. Parse the RTP fixed header, distinguish RTCP by payload type (72–76), expose a plausibility check used by the heuristic flow detector, and `rewrite(sequence, timestamp)` used for loop continuity (REQ-009). Payload bytes are never touched. | ✅ | 2026-08-26 |
| TASK-007 | **Add `rtsp_messages.py`** — an interleaved-aware scanner that walks a byte stream and yields either an RTSP message (request or response, `Content-Length` honoured) or a `$`-framed interleaved frame. Shared by the PCAP extractor and the replay server, so framing is implemented and tested exactly once. | ✅ | 2026-08-26 |
| TASK-008 | **Extend `models.py` / `config.py`** — add a `ReplaySpec` model and a top-level `replays:` list on `CameraStack`, with source-path resolution relative to the config file and uniqueness checks against camera paths. `cameras:` semantics are untouched, so every existing config keeps working. | ✅ | 2026-08-26 |

### Phase 3 — Proxy Mode (deferred)

- GOAL-003: Implement `vcam proxy` — transparent RTSP reverse-proxy with simultaneous PCAP capture.

**Deferred to revision 1.2.** Rationale: the replay path is the one that unblocks reproducing DeepStream faults, and it can be fed today by `tcpdump` on the station (already verified — see `impl-paths-pcap-capture-replay.md`, "Docker / Linux capability constraints"). Building the proxy first would have front-loaded the hardest, least-validated component. TASK-009 … TASK-013 carry over unchanged except for the architecture correction recorded in TASK-003.

### Phase 4 — PCAP Replay Mode

- GOAL-004: Implement `vcam replay` — serve a captured PCAP as a live RTSP stream with wire-level fidelity.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | **Add `replay_source.py`** at `src/vcam/replay_source.py`. Build a `ReplaySource` from a PCAP: (a) reassemble each TCP flow from `Datagram.tcp_seq`, tolerating retransmits and overlap; (b) scan the server→client stream for the RTSP handshake; (c) discover RTP endpoints from each `SETUP` response `Transport` header — `interleaved=a-b` for TCP, `server_port=a-b` for UDP — correlated to the request URI by `CSeq` (REQ-008); (d) collect the RTP packets of every track in capture order; (e) if no handshake is present, fall back to grouping plausible RTP by (flow, SSRC). | ✅ | 2026-08-26 |
| TASK-015 | **SDP reconstruction** — extract the SDP from the captured `DESCRIBE` response and re-emit it with our own connection line and `a=control:` attributes while preserving `m=`, `a=rtpmap` and `a=fmtp` verbatim (the parameter sets are exactly what makes a fault reproducible). Fall back to a payload-type heuristic when the handshake was not captured, and honour a `--sdp FILE` override as the RISK-004 escape hatch. | ✅ | 2026-08-26 |
| TASK-016 | **Add `rtsp_replay.py`** — a minimal threaded RTSP server implementing `OPTIONS`, `DESCRIBE`, `SETUP`, `PLAY`, `PAUSE`, `TEARDOWN`, `GET_PARAMETER` and `SET_PARAMETER`, serving both `RTP/AVP` (UDP) and `RTP/AVP/TCP` (interleaved) per REQ-010, with optional Basic auth reusing `AuthSpec`. Playback merges all tracks into one capture-ordered timeline and paces it with `Pacer`; looping applies per-SSRC seq/timestamp rewriting (REQ-009). | ✅ | 2026-08-26 |
| TASK-017 | **Add `vcam replay` CLI command** in `cli.py`. Options: `--source FILE` (required), `--path TEXT`, `--host`, `--port INT` (default 8554), `--loop / --no-loop`, `--speed FLOAT`, `--sdp FILE`, `--username` / `--password`, `--list-tracks`. | ✅ | 2026-08-26 |
| TASK-022 | **Supervisor integration** — a `replays:` entry in `cameras.yaml` makes the supervisor spawn `vcam replay` as a `ManagedProcess`, inheriting the existing restart backoff, health file and shutdown handling for free (PAT-001). | ✅ | 2026-08-26 |
| TASK-023 | **Refactor `rtp_sender.py`** into a drift-free stoppable `Pacer` (absolute deadlines against a monotonic base, coarse sleep plus a short busy-wait tail, catch-up instead of accumulating lag) plus the UDP `RtpSender` used by the server. | ✅ | 2026-08-26 |

### Phase 5 — Integration & Hardening

- GOAL-005: End-to-end validation, documentation, and Docker packaging.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-018 | **`vcam doctor` checks** — verify the PCAP backend imports and report the resolved scapy version. The revision-1.0 `libpcap` probe is dropped: nothing links against it. | ✅ | 2026-08-26 |
| TASK-019 | **Update docs** — add a `replays:` example to the sample config, and document the replay mode, the `tcpdump` capture recipe, and PCAP credential redaction in the README and cheatsheet. | ✅ | 2026-08-26 |
| TASK-020 | ~~**Dockerfile `libpcap-dev`**~~ — dropped. No C binding is used, and adding `-dev` packages to the runtime image would reverse the CVE reduction done in `5afd55d`. `tcpdump` + `setcap cap_net_raw+eip` is added only when the proxy lands. | ➖ | |
| TASK-021 | **End-to-end deployment test** — capture a real camera with `tcpdump`, verify with `capinfos`, then `vcam replay` it into a DeepStream pipeline and confirm frame delivery with no pipeline errors over a 5-minute window. | ⬜ | |

---

## 3. Alternatives

- **ALT-001**: Use `tcpdump` / `tcpreplay` as external system tools instead of Python-native PCAP I/O. Rejected for replay: `tcpreplay` blasts frames at the wire with the original L2/L3 addressing and no RTSP negotiation, so no consumer would ever attach. `tcpdump` remains the recommended *capture* tool until proxy mode lands.
- **ALT-002**: Re-encode PCAP video to an MP4 and replay via the existing ffmpeg path. Rejected because re-encoding destroys the original packet timing and codec parameters that are needed to reproduce bitstream-level pipeline errors.
- **ALT-003**: Extend ffmpeg with `-f rtsp` and `-use_wallclock_as_timestamps` as a relay. Rejected because ffmpeg re-muxes RTP packets and cannot faithfully reproduce original packet inter-arrival times or fragmentation patterns.
- **ALT-004**: Build a standalone proxy binary in Go (closer to MediaMTX ecosystem). Rejected for scope and maintainability reasons; Python is the project language; a Go sub-binary would fragment the distribution story.
- **ALT-005**: Publish replay into MediaMTX via `source: udp+rtp://…` + `rtpSDP`. This *is* supported in the pinned v1.20.1 (unlike the `rtp://` of revision 1.0) and would have bought reader auth, multi-reader fan-out and transport negotiation for free. **Rejected**: MediaMTX depacketises and re-packetises, replacing SSRC, sequence numbers, RTP timestamps and fragmentation — precisely the properties REQ-003 exists to preserve. A capture whose bug *is* the packetisation would replay as a clean stream. The self-hosted RTSP listener costs one extra module and keeps the feature honest.

---

## 4. Dependencies

- **DEP-001**: ~~`libpcap` / `libpcap-dev`~~ — **removed**. Nothing links against it: `scapy` is pure Python and `tcpdump` bundles its own libpcap runtime. Keeping it would have added `-dev` packages to a runtime image that was deliberately slimmed for CVE reasons.
- **DEP-002**: `scapy>=2.5` — pure-Python PCAP/PCAPNG reader and writer with link-layer decoding. Added to the main dependency list but imported lazily (CON-004) so the CLI hot path is unaffected.
- **DEP-003**: Python stdlib only for the RTSP replay server (`socket`, `socketserver`, `threading`, `secrets`, `base64`). No async framework is introduced — the existing codebase is thread-based and mixing paradigms would complicate the supervisor.
- **DEP-004**: ~~MediaMTX ≥ 1.6 for RTSP ANNOUNCE~~ — **not required by replay**, which never talks to MediaMTX. Still relevant to the deferred proxy work.

---

## 5. Files

- **FILE-001**: `src/vcam/pcap.py` — PCAP I/O: `Datagram`, `iter_datagrams()`, `PcapWriter` (+ `write_udp` / `write_tcp` fixture helpers).
- **FILE-002**: `src/vcam/rtp.py` — RTP/RTCP header parsing, plausibility check, seq/timestamp rewriting.
- **FILE-003**: `src/vcam/rtsp_messages.py` — interleaved-aware RTSP framing shared by the extractor and the server.
- **FILE-004**: `src/vcam/replay_source.py` — PCAP → `ReplaySource` (tracks, SDP, timeline), TCP reassembly, handshake parsing, heuristic fallback.
- **FILE-005**: `src/vcam/rtsp_replay.py` — the minimal RTSP server and paced playback engine.
- **FILE-006**: `src/vcam/rtp_sender.py` — `Pacer` and UDP `RtpSender`.
- **FILE-007**: `src/vcam/models.py` — `ReplaySpec` + `CameraStack.replays`.
- **FILE-008**: `src/vcam/config.py` — resolve replay source paths, sample config entry.
- **FILE-009**: `src/vcam/supervisor.py` — spawn `vcam replay` children for `replays:` entries.
- **FILE-010**: `src/vcam/cli.py` — `vcam replay` sub-command, `doctor` PCAP backend check.
- **FILE-011**: `README.md` / `CHEATSHEET.md` — replay usage, capture recipe, redaction guidance.
- **FILE-012**: `src/vcam/proxy.py` — *deferred*; `RtspProxy` and `ProxyService`.
- **FILE-013**: `tests/test_pcap.py`, `tests/test_rtp.py`, `tests/test_rtsp_messages.py`, `tests/test_replay_source.py`, `tests/test_rtsp_replay.py`, `tests/test_rtp_sender.py`.

---

## 6. Testing

- **TEST-001**: `PcapWriter` round-trips synthetic UDP and TCP frames; `iter_datagrams` recovers payloads, endpoints and timestamps exactly. (FILE-013)
- **TEST-002**: `iter_datagrams` raises `PcapError` on a missing, corrupt or truncated PCAP and does not hang. (FILE-013)
- **TEST-003**: `Pacer` respects inter-packet gaps within 5 ms over a synthetic sequence, does not accumulate drift when a send runs late, and aborts promptly on `stop()`. (FILE-013)
- **TEST-004**: The RTSP scanner splits a stream mixing request, response-with-body and `$`-framed interleaved data, and tolerates a message arriving in fragments. (FILE-013)
- **TEST-005**: `ReplaySource` built from a UDP-transport capture discovers the RTP ports from the `SETUP` responses — *not* from a hard-coded 5004 — and collects each track's packets in order. (FILE-013)
- **TEST-006**: `ReplaySource` built from a TCP-interleaved capture reassembles the TCP flow (including a retransmitted segment) and extracts RTP from the `$` channels. (FILE-013)
- **TEST-007**: SDP is taken from the captured `DESCRIBE` response with `rtpmap`/`fmtp` preserved verbatim, and `--sdp` overrides it. (FILE-013)
- **TEST-008**: With no handshake in the capture, the heuristic groups plausible RTP by (flow, SSRC) and synthesises a payload-type-based SDP. (FILE-013)
- **TEST-009**: Loop rewriting keeps sequence numbers and RTP timestamps monotonic across the loop boundary, wraps correctly at 16/32 bits, and leaves the RTP payload byte-identical. (FILE-013)
- **TEST-010**: End-to-end — an in-process RTSP client performs `OPTIONS`/`DESCRIBE`/`SETUP`/`PLAY` against the replay server over TCP interleaved and receives the captured RTP packets byte-for-byte, in order. Repeated for UDP transport. (FILE-013)
- **TEST-011**: Basic auth rejects an anonymous `DESCRIBE` with `401` + `WWW-Authenticate`, and accepts valid credentials. (FILE-013)
- **TEST-012**: Config loader parses a `replays:` entry, resolves its source relative to the config file, and rejects a replay path colliding with a camera path. (FILE-013)
- **TEST-013**: End-to-end deployment integration — replay delivers frames to DeepStream without pipeline errors for 5 minutes. (TASK-021, manual)

---

## 7. Risks & Assumptions

- **RISK-001**: ~~libpcap availability on arm64~~ — **eliminated** by dropping the dependency (DEP-001).
- **RISK-002**: **RTSP interleaving complexity** — real cameras commonly use TCP-interleaved RTSP (`$` framing). This affects replay as much as the deferred proxy, and a naive chunked read does not preserve packet boundaries. Mitigation: framing lives in one tested module (`rtsp_messages.py`) used by both sides, with TCP reassembly that tolerates retransmission and overlap. (TEST-004, TEST-006)
- **RISK-003**: **Replay timing drift at high bitrates** — for cameras streaming at ≥ 10 Mbps, `time.sleep` jitter (~1–5 ms) introduces drift over long replays. Mitigation: `Pacer` schedules against absolute deadlines derived from one monotonic base rather than sleeping per gap, busy-waits the final ~1.5 ms, and catches up instead of accumulating lag when a send runs late.
- **RISK-004**: **SDP mismatch on replay** — if the capture started after `SETUP`, SDP reconstruction falls back to a payload-type guess and a strict consumer may reject the stream. Mitigation: document capturing before the session starts, and provide `--sdp FILE`. `vcam replay --list-tracks` prints what was discovered without starting a server.
- **RISK-005**: **Credential leakage in PCAP files** — RTSP `Authorization` / `WWW-Authenticate` headers and `rtsp://user:pass@host` URIs are recorded verbatim. Mitigation: the replay loader warns when it sees any of them, and the README documents `editcap` redaction.
- **RISK-006**: **No RTCP sender reports** — the replay server does not emit RTCP SR, so a consumer relying on RTCP for wall-clock synchronisation across tracks may drift. Acceptable for the single-video-track case this feature targets; RTSP-level keepalives (`OPTIONS` / `GET_PARAMETER`) are answered so sessions do not time out. Revisit if a multi-track A/V capture needs lip-sync.
- **RISK-007**: **Memory footprint** — the current loader holds a capture's RTP packets in memory to keep the timeline simple. A 60 s 10 Mbps capture is ~75 MB, which is fine on a 15 GB Jetson, but a multi-hour capture is not. Mitigation: documented limit; streaming re-read from disk per loop is the escape hatch if it becomes a problem.
- **ASSUMPTION-001**: ~~MediaMTX supports RTSP ANNOUNCE~~ — no longer relevant to replay.
- **ASSUMPTION-002**: The target real IP cameras use standard RTP over TCP or UDP; proprietary RTSP extensions may not be transparently relayed without additional parsing (deferred proxy concern).
- **ASSUMPTION-003**: A capture may contain multiple tracks of a single RTSP session; multiple *concurrent sessions* in one PCAP are out of scope for v1 — the loader takes the first session it finds and logs the others.

---

## 8. Related Specifications / Further Reading

- [MediaMTX RTSP publish documentation](https://github.com/bluenviron/mediamtx#rtsp)
- [RFC 2326 — Real Time Streaming Protocol (RTSP)](https://www.rfc-editor.org/rfc/rfc2326)
- [RFC 3550 — RTP: A Transport Protocol for Real-Time Applications](https://www.rfc-editor.org/rfc/rfc3550)
- [RFC 4571 / RFC 2326 §10.12 — RTP interleaved in RTSP (`$` framing)](https://www.rfc-editor.org/rfc/rfc2326#section-10.12)
- [RFC 6184 — RTP Payload Format for H.264 Video](https://www.rfc-editor.org/rfc/rfc6184)
- [pcapng file format specification](https://pcapng.com/)
- [Wireshark / editcap — credential redaction](https://www.wireshark.org/docs/man-pages/editcap.html)
- [scapy PCAP read/write documentation](https://scapy.readthedocs.io/en/latest/usage.html#reading-and-writing-pcap-files)
- Existing vcam modules: `src/vcam/supervisor.py`, `src/vcam/models.py`, `src/vcam/mediamtx.py`

---

## 9. Deferred to revision 1.2 — proxy mode

Proxy mode (GOAL-003, TASK-009 … TASK-013) is intentionally not implemented in this revision. Until it lands, captures are produced on the station with `tcpdump`, whose Docker capability requirements are already verified in `impl-paths-pcap-capture-replay.md`:

```bash
tcpdump -i eth0 -w /captures/cam1.pcap host <camera_ip> and tcp port 554
```

When the proxy is picked up, the architecture must be the corrected one from TASK-003 — an RTSP server downstream and an RTSP client upstream, with request-URI and `Transport` rewriting — not the revision-1.0 byte pipe. It can reuse `rtsp_messages.py` for framing and `PcapWriter.write_tcp()` / `write_udp()` for producing captures that this replay path can already read.
