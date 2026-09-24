# Clock synchronisation (RTCP NTP timestamps)

Every RTCP Sender Report carries a wall-clock NTP timestamp, which is what the downstream pipeline (e.g. EAIS DeepStream) uses for clock-sync diagnostics.  That timestamp comes directly from the host system clock — there is no independent clock inside vcam.

## RTCP clock chain

```
host OS clock  →  MediaMTX time.Now()  →  RTCP SR NTPTime  →  downstream client
```

## Syncing the container to an NTP server

When running inside Docker, the container has its **own Linux kernel clock** (separate from the macOS host on Docker Desktop) that can be independently adjusted with `CAP_SYS_TIME`.

```yaml
# docker-compose.yml
services:
  vcam:
    image: vcam:latest
    cap_add:
      - SYS_TIME     # grants adjtimex / clock_settime inside the container
    command: run --ntp-server 192.168.198.151   # EAIS station IP
```

Or via the config file:

```yaml
server:
  ntp_server: 192.168.198.151   # sync before start; container + SYS_TIME required
```

Before the RTSP server starts, vcam queries the NTP server (pure Python, no extra dependencies), measures the offset, and applies it:
- **|offset| ≤ 128 ms** → gradual slew via `adjtimex(ADJ_SETOFFSET)` (no timestamp jump on live streams)
- **|offset| > 128 ms** → instant step via `clock_settime`

## Checking clock status

```bash
# Read-only — works anywhere, no privileges needed
vcam clock-status --ntp-server 192.168.198.151
# System time  : 2026-08-25T10:08:34 UTC
# In container : yes
# CAP_SYS_TIME : yes
# NTP server   : 192.168.198.151
# Offset       : +1.853 ms
# RTT          : 0.812 ms
```

## Why NTP sync is container-only

On a bare CLI or systemd service the system clock is shared with the rest of the machine.  Adjusting it would affect every other process, so `--ntp-server` is rejected outside a container.  Use the host's existing NTP daemon (chrony / timesyncd) if you need whole-system sync.

## Testing clock skew impact

| Scenario | Setup |
|---|---|
| Well-synced camera | `--ntp-server <eais-ip>` + `cap_add: [SYS_TIME]` |
| Skewed camera | Disable NTP in the container (`timedatectl set-ntp false`) |
| Fixed offset | `timedatectl set-time` inside the container after disabling NTP |
| Free-running drift | Leave the container clock unsynced with no NTP daemon |
