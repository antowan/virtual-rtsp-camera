# Clock synchronisation (RTCP NTP timestamps)

Every RTCP Sender Report carries a wall-clock NTP timestamp, which downstream clients use for clock-sync diagnostics. MediaMTX reads the system clock visible to its process; vcam does not have an independent clock.

## RTCP clock chain

```
host OS clock  →  MediaMTX time.Now()  →  RTCP SR NTPTime  →  downstream client
```

## Adjusting the visible system clock from an NTP server

Containers do not generally have an independent system clock. Docker Desktop runs containers inside a Linux VM, whose clock is separate from the macOS or Windows host. On native Linux, containers share the host kernel clock; granting `CAP_SYS_TIME` may therefore adjust the host clock as well. Do not add this capability on native Linux unless changing the host's system clock is explicitly intended.

```yaml
# docker-compose.yml
services:
  vcam:
    image: vcam:latest
    # Only for a Docker Desktop/isolated Linux VM, or when changing the
    # native Linux host clock is explicitly intended.
    cap_add:
      - SYS_TIME     # grants adjtimex / clock_settime on the visible kernel clock
    command: run --ntp-server 192.0.2.123   # example NTP server; replace with your server
```

Or via the config file:

```yaml
server:
  ntp_server: 192.0.2.123   # example; isolated VM + SYS_TIME required
```

Before the RTSP server starts, vcam queries the NTP server (pure Python, no extra dependencies), measures the offset, and applies it:
- **|offset| ≤ 128 ms** → gradual slew via `adjtimex(ADJ_SETOFFSET)` (no timestamp jump on live streams)
- **|offset| > 128 ms** → instant step via `clock_settime`

## Checking clock status

```bash
# Read-only — works anywhere, no privileges needed
vcam clock-status --ntp-server 192.0.2.123
# System time  : 2026-08-25T10:08:34 UTC
# In container : yes
# CAP_SYS_TIME : yes
# NTP server   : 192.0.2.123
# Offset       : +1.853 ms
# RTT          : 0.812 ms
```

## Where clock adjustment is safe

vcam rejects `--ntp-server` outside a container because adjusting a bare process's clock would affect the whole machine. This container check does **not** prove the container has an isolated kernel clock: on native Linux, use the host's NTP daemon (chrony / timesyncd) instead. Only grant `SYS_TIME` when the container runs in an isolated VM or when changing the host clock is explicitly intended.

## Testing clock skew impact

| Scenario | Setup |
|---|---|
| Well-synced camera | Use the host's NTP daemon; in an isolated VM, `--ntp-server <eais-ip>` + `cap_add: [SYS_TIME]` |
| Skewed camera | Use a disposable isolated VM and disable its NTP service |
| Fixed offset | Adjust time only in a disposable isolated VM |
| Free-running drift | Leave a disposable isolated VM unsynced with no NTP daemon |
