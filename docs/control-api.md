# Control a running VCAM engine

The optional control server lets clients change the streams owned by a running
`vcam run`. RTSP carries video; the **VCAM** HTTP API carries commands and
observations. Do not use MediaMTX's private `/v3` endpoints as an alternative:
those changes bypass VCAM's configuration, retry logic and process ownership.
An ordinary `vcam run` opens no control listener.

## Start locally

From the source checkout:

```bash
uv sync --extra control
# cameras.yaml can start empty:
# control: {enabled: true}
# cameras: []
uv run --extra control vcam run -c cameras.yaml --control
```

In a second terminal, from the config directory:

```bash
uv run --extra control vcam status
uv run --extra control vcam add ./clip.mp4 --name lobby -c cameras.yaml
uv run --extra control vcam urls -c cameras.yaml
# Stop publishing, not the daemon. Clients can still read status or start it again.
uv run --extra control vcam engine stop --allow-disruption
uv run --extra control vcam engine start
```

`control.enabled: true` in YAML enables the same server for the installed
service (`run -c …`). `--control-port` / `control.port` are distinct from the
private MediaMTX `--api-port`. The default endpoint is `http://127.0.0.1:8765`;
set `control.port: 0` to allocate a free port. Local discovery looks up this
specific config, not the first server on a well-known port.

For a remote engine, pass the endpoint and a protected token file explicitly.
The media path is on the **engine**, not the client. For example:

```bash
vcam add clips/side.mp4 --name side \
  --endpoint https://camera-host.example:8765 \
  --token-file ~/.config/vcam/operator.token --ca-file ./control-ca.pem --json
```

`--no-wait` returns the accepted operation without waiting for its result.
`VCAM_ENDPOINT` (also `VCAM_CONTROL_ENDPOINT`) and `VCAM_CONTROL_TOKEN_FILE`
are alternatives to the command-line options. An explicit endpoint that is
unreachable, incompatible, or rejects the credential is an error, **not**
permission to edit a local file. `--offline` selects local authoring only;
it cannot be combined with an explicit endpoint or override a live owner's lock.

`add`, `list`, `show`, and `urls` discover the engine for the selected local
config. `probe` and `doctor` use local tools by default; pass an endpoint or
`-c cameras.yaml` to run diagnostics on the selected engine. An explicit `-c`
for diagnostics requires a running owner. Select either a config or an explicit
endpoint, not both. Use `--json` for machine-readable output. Online `show`
redacts credentials; online `urls --all` includes disabled streams without
exposing passwords.

For an inline launch such as `vcam run --source ./clip.mp4 --control`, use the
printed endpoint and private token-file path with the client. These changes are
**runtime-only**: `config.persistence` is `ephemeral`, the disk revision and
config path are `null`, and reload is unavailable. The per-process token is
removed on clean exit. A read-only YAML deployment still supports status and
transient actions; a failed file write never changes the running streams.

## Credentials and remote clients

All `/api/v1` endpoints, including readiness, OpenAPI and events, require
`Authorization: Bearer <token>`. VCAM generates a private per-config token on
first use. To supply your own, set `control.token_file` to a regular file owned
by the service user with owner-only permissions and one random token on a
single line. Do not store the token in YAML or put it in argv, a URL or a log.
To rotate it, stop the daemon, replace the token file securely and restart.

The live descriptor is separate from the token. It records the actual endpoint,
descriptor version, config path, per-start instance ID, and credential file
locations. Clients verify the API major version against `/status`. The descriptor
disappears on clean shutdown; the private credential survives.
On macOS the directory is `~/Library/Application Support/vcam/control/`; on
Linux it is `${XDG_STATE_HOME:-~/.local/state}/vcam/control/`. `VCAM_STATE_DIR`
overrides the **parent** directory. Processes sharing a writable config must
also share the state directory and OS user; this is not a distributed lock.

Prefer a local connection or an SSH tunnel. An actual LAN listener requires
explicit opt-in, TLS and a certificate valid for the name clients use:

```yaml
control:
  enabled: true
  host: 0.0.0.0
  port: 8765
  allow_remote: true
  allowed_hosts: [camera-host.example]
  tls_cert: certs/control.crt
  tls_key: certs/control.key
  # Only if a separate browser origin should be able to call the API:
  allowed_origins: [https://console.example]
```

Add any client-facing Host name used with a wildcard bind to `allowed_hosts`;
list exact browser origins (including non-default ports). Neither setting
accepts wildcards. Clients must validate TLS, including the hostname, and
receive their own copy of the bearer credential over a trusted channel.
For local discovery through TLS, include the discovered loopback address/name in
the certificate if the listener binds to a wildcard. Never publish the private
MediaMTX API. The token represents one trusted operator of the service account,
not a multi-tenant authorization system: constrain the service user's filesystem
and network access. [Docker](docker.md#controlling-a-running-container) and
[service](service.md) have deployment examples.

## REST contract

Fetch `GET /api/v1/openapi.json` with the same credentials for the machine
contract; `GET /api/v1/schema/config` returns the Pydantic config schema.
The interactive Swagger/Redoc pages are not enabled. Paths below are relative
to `/api/v1`.

| Endpoint | Use |
| --- | --- |
| `GET /status`, `/health`, `/ready` | Engine identity/revisions and observed state; stream health; control command readiness. `/ready` remains available when orchestration is intentionally stopped or empty. |
| `GET /cameras`, `/cameras/{port}/{name}`, `/urls` | Camera definitions, observed status, and credential-free playback URLs. Names need only be unique **on the same RTSP port**. `/urls` excludes disabled streams unless `include_disabled=true`. |
| `POST /cameras`, `PATCH` or `DELETE /cameras/{port}/{name}` | Create, merge supplied fields, or remove a camera. `{"enabled":false}` disables persistently. File paths are interpreted on the engine host, relative to the config; files are not uploaded. |
| `POST /cameras/{port}/{name}/actions` | `{"action":"start"}`, `stop` or `restart`: change transient playback without editing YAML. |
| `POST /engine/actions` | Start, stop or restart stream orchestration. Stop/restart require `"allow_disruption":true`. No action launches or terminates the daemon process. |
| `GET /replays`, `/replays/{port}/{name}`, `/sync-groups`, `/sync-groups/{name}` | Read-only views of advanced streams and groups. Use a full configuration plan for disruptive changes. |
| `GET /config` | Redacted declared/effective settings, override provenance and revisions. Not a raw YAML editor/export. Never submit the redacted result as replacement YAML. |
| `POST /config/plan`, `/config/reload`, `/config/apply` | Preview a full proposed YAML document (`{"text":"…"}`) or the edited local file, then apply its `plan_id` using its `base_revision`. Disruptive changes require explicit confirmation. Reload alone does not write or change streams. |
| `GET /operations/{id}`, `/logs`, `/events` | Poll completion, get recent backend logs, or subscribe to authenticated `text/event-stream` updates. |
| `POST /diagnostics/doctor`, `/diagnostics/probe` | Read-only backend capability check or bounded source inspection. Probe accepts `{"source":"clip.mp4"}` or `{"camera":{"port":8554,"name":"lobby"}}`. No caller-chosen executable or installation. |

**Write sequence.** On startup, wait for `GET /ready` to return `200`. Status is
available earlier; a valid camera edit before the first supervisor snapshot
returns `503` (`engine_starting`). Read the current config and retain its `ETag`. Send it in
`If-Match` with camera mutations, document proposals, and plan application. A
missing revision returns `428` and a stale one `412`; refetch instead of
overwriting someone else's edit. Use `Idempotency-Key` to retry the *same*
operation in this daemon's retained window. It is not an exactly-once guarantee
across a restart. Reusing the same key for a different action or payload
returns `422 idempotency_key_reused`. Configuration previews return a plan; accepted runtime work
returns `202`, an operation object and a `Location` to poll. A terminal `failed`
operation is a failure even if the initial request returned `202`.

```python
from pathlib import Path
from vcam.control import read_descriptor, read_token_file
from vcam.control_client import ControlClient

descriptor = read_descriptor(Path("cameras.yaml").resolve())
if descriptor is None:
    raise RuntimeError("no controlled owner")
# Local plaintext example; supply a verified SSLContext for private TLS.
client = ControlClient(
    descriptor["endpoint"], token=read_token_file(Path(descriptor["token_file"]))
)
assert client.request("GET", "status")["instance_id"] == descriptor["instance_id"]
config = client.request("GET", "config")
op = client.request(
    "POST",
    "cameras",
    {"name": "side", "source": "side.mp4"},
    revision=config["revision"],
    idempotency_key="add-side-once",
)
result = client.wait(op["id"])
```

Errors have the shape `{"error":{"code":"…","message":"…","request_id":"…","details":{}}}`.
Check the operation's `config_persisted`, `applied` and error details:
atomic replacement of the YAML and changing child processes are different
steps. Enabled local files are inspected before replacement; a missing file,
occupied listener, or media change after inspection does not update the YAML.
Changing control or host-clock settings requires a local daemon restart.
If persistence succeeded but runtime application failed, the new desired
configuration remains on disk. Inspect status and correct or reapply; do not
blindly retry the original edit. A successfully applied camera can still be
waiting for its upstream media source; check health or decode the RTSP feed
when playback readiness matters. A camera's runtime `generation` changes
when the publisher is replaced. Path readiness is sampled asynchronously;
`unknown` is not evidence that a newly started stream has decoded a frame.

Granular edits preserve existing YAML comments and relative source paths;
alias-based documents may require an explicit full-document plan. The config
directory must be writable for atomic replacement. Out-of-band edits are not
auto-applied: preview with reload and explicitly apply the plan. Advisory locks
protect cooperating VCAM writers, not external editors that ignore them.

## Web and desktop integration

Use the OpenAPI contract and authenticated streaming `fetch` for SSE; native
browser `EventSource` cannot attach a bearer header. Keep tokens out of query
strings and browser-persistent storage. Reconnect with `Last-Event-ID` using
the last dispatched ID. If the history expired, the instance changed, or a
subscriber receives `resync`, fetch fresh snapshots before resubscribing.
For shell clients, `vcam events --json` writes one JSON object per event to
stdout as it arrives, including when piped. Use `--since <id>` to resume.
Browser playback of RTSP is **not** enabled by the control API.

A Tauri client can store credentials in the OS-protected credential store
and call the same API. Window closure should only close subscriptions, not
stop the backend. Use the local OS service manager or Docker for daemon
lifecycle, independently of every connected client.
