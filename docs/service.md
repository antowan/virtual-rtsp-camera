# Running as a service

`vcam service` installs the stack as a long-running background service — no root required.

| Platform | Backend | Unit file |
|---|---|---|
| Linux | systemd (user session) | `~/.config/systemd/user/vcam.service` |
| macOS | launchd (LaunchAgent) | `~/Library/LaunchAgents/vcam.plist` |

```bash
# 1. Create a config if you don't have one yet
vcam init -s videos/cam1.mp4

# 2. Install and start immediately
vcam service install                  # uses ./cameras.yaml by default
vcam service install -c /abs/path/cameras.yaml   # explicit config

# 3. Day-to-day operations
vcam service status
vcam service stop
vcam service start
vcam service logs                     # streams the log (Ctrl-C to exit)
vcam service uninstall
```

The service runs `vcam run -c <absolute config>` and keeps it alive automatically
(restarts after crashes).  Put all camera options — ports, modes, codecs — in the
YAML file.

## Linux notes

The service runs under your **user systemd** session (`systemctl --user`), so it
starts when you log in and stops when you log out.

On headless servers (the EdgeAI Jetson, CI boxes) you usually want it to survive
logout.  Enable lingering once:

```bash
sudo loginctl enable-linger $USER
```

Logs are written to the systemd journal:

```bash
journalctl --user -u vcam.service -f
```

## macOS notes

The LaunchAgent starts at login and is restarted automatically on crash.
Logs land in `~/Library/Logs/vcam-vcam.log` (or `vcam-<name>.log` for a custom
`--name`).

If ffmpeg is installed via Homebrew (i.e., in `/opt/homebrew/bin`) you may need
to install vcam with the same shell so that PATH is captured correctly:

```bash
vcam service install         # run from a shell where `which ffmpeg` returns a path
```

## Distribution notes

The recommended install method is via [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/), both of which produce a standalone vcam executable
that the service unit can reference by absolute path:

```bash
uv tool install .            # installs vcam into its own isolated env
# or
pipx install .

vcam service install
```

A `.deb` package or Homebrew formula is optional and only worth adding if you need
a system-wide `apt install` workflow (e.g., managed fleet deployment).
