# Capture replay

Simulation modes fake a *class* of fault. Sometimes you need the exact one: the RTP
stream that made a specific decoder fall over on a specific site, on a Tuesday. For that,
capture the real camera once and replay the packets.

```bash
# 1. capture the camera's traffic (on a host that sees it)
sudo tcpdump -i eth0 -s 0 -w camera.pcap host 192.168.1.64 and tcp port 554

# 2. inspect what is in there
uv run vcam replay camera.pcap --list-tracks

# 3. serve it
uv run vcam replay camera.pcap --port 8600 --path cam1
# -> rtsp://127.0.0.1:8600/cam1
```

Replay does **not** go through MediaMTX or ffmpeg. `vcam` serves the capture from its own
minimal RTSP server and puts the recorded packets on the wire untouched — same payload
bytes, same SSRC, same sequence numbers, same fragmentation, same inter-packet timing.
Anything that re-packetises the stream (MediaMTX included) would sanitise away exactly the
packet-level artifacts you captured it for.

What a capture needs to contain:

- **The RTSP handshake**, ideally. `vcam` reads the `DESCRIBE` response for the SDP and the
  `SETUP` exchanges for the RTP ports or interleaved channels. For UDP, it matches packets to
  the negotiated source and destination endpoints, so unrelated traffic is not replayed.
- If you only have the media, pass the session description yourself with
  `--sdp camera.sdp`; `vcam` then finds the RTP streams heuristically.

What replay deliberately does **not** do:

- **No `PAUSE`.** It answers `501`: resuming would restart the timeline with backwards RTP
  counters, which is the discontinuity looping exists to avoid. Stop and reconnect instead.
- **No seeking**, and no RTCP Sender Reports — so a multi-track capture carries no
  RTP-to-wall-clock mapping for A/V sync.
- **No session enforcement.** Any reader on a connection can drive it; this is a lab tool.
- **No huge captures.** Loading materialises the packets several times over — measured at
  roughly **6× the file size in RAM, at ~15 MB/s** — so anything above 256 MB (about 1.5 GB
  of memory) is refused rather than risking an OOM kill. Trim it (`editcap -A/-B`, or a
  tighter `tcpdump` filter), or raise `$VCAM_MAX_CAPTURE_BYTES` if you have the headroom.

Both transports are handled: RTP-over-UDP and `RTP/AVP/TCP` interleaved inside the RTSP
connection (which is what most IP cameras actually use). Readers choose their own transport
independently of how the capture was made.

| flag | what it does |
| --- | --- |
| `--port` | port for this replay's own listener — **required to be free**; a replay never shares the MediaMTX port |
| `--path` | RTSP path (defaults to the capture's file stem) |
| `--loop` / `--no-loop` | repeat forever (default) or serve once and stop |
| `--rewrite-on-loop` | keep sequence numbers and RTP timestamps monotonic across loops (default) |
| `--speed` | playback multiplier; `2.0` replays twice as fast |
| `--sdp` | session description to use when the capture has no `DESCRIBE` |
| `--username` / `--password` | require basic auth from readers (or set the secret in `$VCAM_REPLAY_PASSWORD`, which is how `vcam run` passes it so it never reaches the process list) |
| `--list-tracks` | describe the capture and exit without serving |

**Looping rewrites headers on purpose.** A raw rewind sends sequence numbers and
timestamps backwards, and decoders stall on it (`ffmpeg` reports *non monotonically
increasing dts*). By default each loop is offset past the previous one, so the stream runs
indefinitely while the *packets themselves* stay byte-identical. Real loss gaps inside the
capture are preserved. Use `--no-rewrite-on-loop` when the rewind is the thing you want to
reproduce.

> **Captures contain credentials.** RTSP `Authorization` headers and `rtsp://user:pass@host`
> URIs are recorded in plain text. `vcam replay --list-tracks` warns when it finds them.
> Strip them before sharing a capture: `editcap -E 0 in.pcap out.pcap`, or re-capture
> against a camera with throwaway credentials.

In YAML, replays sit next to cameras and each one needs its own port:

```yaml
replays:
  - name: broken-cam
    source: captures/broken-cam.pcap   # relative paths resolve next to this file
    port: 8600               # own listener; must not collide with a camera port
    loop: true
    # sdp: captures/broken-cam.sdp   # only if the capture has no DESCRIBE
    # speed: 1.0
    # rewrite_on_loop: true
    # enabled: true
```

`vcam run` starts replays alongside cameras, and `vcam list` / `vcam urls` / the health file
report them. A stack of only replays starts no MediaMTX at all.
