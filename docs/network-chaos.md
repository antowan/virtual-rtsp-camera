# Network chaos testing (Toxiproxy)

While simulation modes fake video-level artifacts (such as noise or freezes), you can test
**network-layer transport failures** (latency spikes, bandwidth starvation, silent TCP disconnects)
by running an optional [Toxiproxy](https://github.com/shopify/toxiproxy) sidecar.

```
+-------------+                  +-------------+                  +-------------+
| RTSP Client | --(RTSP/TCP)-->  |  Toxiproxy  | --(RTSP/TCP)-->  |    vcam     |
|  (Consumer) |  rtsp://...:8654 |  (Port 8654)|  vcam:8554       | (MediaMTX)  |
+-------------+                  +-------------+                  +-------------+
                                        ^
                                        | HTTP API (:8474)
                                 [ QA / CI script ]
```

## 1. Start the chaos profile

```bash
docker compose --profile chaos up -d
```

This starts `vcam` on `:8554` and Toxiproxy with its HTTP API on `:8474`.

## 2. Create the RTSP proxy

Point Toxiproxy on `:8654` to upstream `vcam:8554` via `curl` (or `toxiproxy-cli`):

```bash
curl -s -X POST http://localhost:8474/proxies \
  -H "Content-Type: application/json" \
  -d '{"name": "rtsp_cam", "listen": "0.0.0.0:8654", "upstream": "vcam:8554"}'
```

Now connect your RTSP consumer or pipeline to `rtsp://localhost:8654/cam1`.

## 3. Inject fault scenarios (toxics)

Toxics can be injected and removed dynamically while clients are streaming:

- **Latency & jitter:**
  ```bash
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam/toxics \
    -H "Content-Type: application/json" \
    -d '{"type": "latency", "name": "lag", "attributes": {"latency": 500, "jitter": 100}}'
  ```
- **Bandwidth rate limit (KB/s):**
  ```bash
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam/toxics \
    -H "Content-Type: application/json" \
    -d '{"type": "bandwidth", "name": "throttle", "attributes": {"rate": 100}}'
  ```
- **Silent timeout (stalled connection):**
  ```bash
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam/toxics \
    -H "Content-Type: application/json" \
    -d '{"type": "timeout", "name": "stall", "attributes": {"timeout": 0}}'
  ```
- **Abrupt disconnect / reconnect test:**
  ```bash
  # Close connections immediately:
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam -d '{"enabled": false}'
  # Re-enable proxy to allow client reconnection:
  curl -s -X POST http://localhost:8474/proxies/rtsp_cam -d '{"enabled": true}'
  ```

## 4. Reset or clean up toxics

```bash
# Remove a single toxic:
curl -s -X DELETE http://localhost:8474/proxies/rtsp_cam/toxics/lag

# Reset all proxies and clear all toxics:
curl -s -X POST http://localhost:8474/reset
```
