# 🛡️ Sentinel — Ultimate Production Engineering Blueprint (`over-engineer.md`)

> **Core Philosophy: Over-Engineer the System, Never the Function.**  
> Stop having to remember every tiny hardware, network, AI, security, and UI edge case yourself. Sentinel is architected so each module handles **one job well**, communicates over typed contracts, and degrades gracefully without cascading failures.

---

## Architecture: Single-Responsibility Pipeline vs. Monolithic Trap

### ❌ What Never To Build (`detectPerson()` doing 10 jobs)
```text
detectPerson()
 ├─ detect face
 ├─ send Telegram/MQTT/Webhook inline (blocks stream on slow network)
 ├─ write logs & disk cleanup inline
 ├─ retry HTTP calls
 ├─ update UI state directly
 ├─ calculate analytics
 └─ 10 other side effects 💀
```

### ✅ The Sentinel Event-Driven Pipeline
```text
[Hardware Sensors & OV2640]  (ESP32-WROOM GPIO 27/14 + ESP32-CAM PWDN/DVP)
            │
            ▼
   [Ingestion & Transport]   (MJPEG :81, UDP :5055, HTTP :80, ESP-NOW Ch 1 Fallback)
            │
            ▼
  [Detection & Temporal AI]  (YuNet → 512-D ArcFace / YOLO11 via NPU / iGPU / CPU)
            │
            ▼
     [Typed Event Bus]       (Immutable Event Envelope + Sequence ID + Dedup Key)
      ├──► [Storage Engine]       (SQLite WAL EventDB + Paced MP4 Recorder + Janitor)
      ├──► [Notification Engine]  (Per-Provider Async Queues + Cooldown + Backoff)
      └──► [Dashboard State API]  (Truthful 3-State Hardware UI + Audio Chimes + SSE/Poll)
```

**Golden Rule:** Every component owns **one** responsibility. A slow Telegram webhook never drops an MJPEG frame; a standby camera never triggers false "Camera Offline" alerts; an NPU/GPU hiccup never stops recordings or crashes the server.

---

## 🔌 1. Hardware (`firmware/esp32-cam/`)

### Power & Brownout Integrity (`src/core/system.cpp`)
- **Problem with silencing brownout (`WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0)`):** Disabling the brownout detector avoids resets during Wi-Fi/OV2640 surge spikes, but leaves flash/PSRAM vulnerable to silent bit corruption on real battery drops.
- **Target Hardening:**
  1. **Staggered Peak-Current Scheduling:** Sequence high-current brings-ups by `20–40 ms` (`PWDN LOW` → OV2640 init → soft-start Flash LED PWM ramp `0→target` over 40 ms → Wi-Fi full-power TX burst) so inrush spikes never overlap.
  2. **Reset-Reason Telemetry:** Read `esp_reset_reason()` at boot (`ESP_RST_POWERON`, `ESP_RST_BROWNOUT`, `ESP_RST_TASK_WDT`, `ESP_RST_PANIC`) and publish it in `/status` + boot event extras so unexpected power dips are immediately visible in the dashboard.
  3. **Li-Ion Battery Monitoring (WROOM):** Read battery rail via ADC voltage divider (averaged over 16 samples with EMA smoothing) and emit `EVENT_LOW_BATTERY` at `< 3.45 V` before brownout occurs.

### Sensor Signal Integrity & Debouncing (`src/sensors/reed.cpp`, `src/sensors/pir.cpp`)
- **Door Reed Switch (`GPIO 27`):**
  - **Two-Stage Hybrid Filter:** Keep the `< 1 ms` leading-edge `IRAM_ATTR` ISR (`onReedIsr`) waking `sensorTaskCore0` for instant `DOOR_OPEN` response, followed by a 3-sample majority read (`2 ms` apart) and hysteresis lock-out (`CFG_REED_DEBOUNCE_MS`) to reject contact bounce and EMI false edges.
  - **Chatter / Tamper Detection:** Count raw transitions within a 3-second sliding window; `> 8` transitions emits `EVENT_TAMPER_SUSPECTED`.
  - **Door Open Timeout:** Already tracks `EVENT_DOOR_TIMEOUT` if left open beyond threshold.
- **PIR Motion (`GPIO 14`):**
  - Strictly gated on `isDoorOpen()` so ambient movement never wakes the camera or pollutes recordings when the door is shut.
  - Enforces warmup holdoff (`CFG_PIR_WARMUP_MS`) and post-trigger cooldown (`CFG_PIR_COOLDOWN_MS`).

### Camera Reliability & Standby Lifecycle (`src/camera/camera_manager.cpp`, `streaming.cpp`)
- **Serialized State Machine:** `STANDBY` ↔ `WAKING` ↔ `READY` ↔ `FAULT_RECOVERY` protected by `s_camera_mutex` at the very start of `initCamera()`, `setCameraSensorActive()`, and `captureSnapshotFb()` so concurrent requests (`/capture`, `requestCameraWakeAsync`, `:81/stream`, 4 s retry) can never collide with `0x103` (`ESP_ERR_INVALID_STATE`).
- **Auto-Standby Guarantee:** Boot in `initCameraStandby()` (`PWDN = HIGH`); `DOOR_OPEN` arms a `30 s` safety ceiling shortened to `8 s` after `DOOR_CLOSED`; manual dashboard wakes (`Low-Power: OFF`) cancel auto-standby until toggled back ON.
- **DVP DMA Stall Recovery & XCLK Guard:** Tune camera XCLK away from active Wi-Fi channel harmonics (`autoTuneCameraXclk`) and trigger a full `250 ms` `PWDN` power cycle if `esp_camera_fb_get()` returns `NULL` 5 times consecutively.

### Thermal & Fan Management (`src/camera/cooling.cpp`)
- **TSENS Calibration & Smoothing:** Apply ESP32-D0WD `-26.0°C` offset with EMA filtering so ADC jitter never hunts fan speed.
- **Standby Silent Curve:** When `!isCameraSensorActive()` and die temperature `< 52°C`, drop fan PWM to `0%`. Apply a `300 ms` `100%` kickstart pulse whenever spinning up from `0%`.
- **Thermal Runaway Guard:** Above `78°C`, automatically cap resolution (`SVGA/UXGA → VGA/QVGA`), clamp FPS to `10`, and zero out Flash LED brightness.

### Hardware & Task Watchdogs (`esp_task_wdt`)
- Arm `esp_task_wdt_init(15, true)` watching `loop()`, `mjpegStreamTask`, `sensorTaskCore0`, and `wroomLinkWorkerTask`, resetting cleanly before any deadlock can hang a remote node.

---

## 📡 2. Communication (`wifi_manager.cpp`, `wroom_cam_link.cpp`, `web_api.cpp`, `server.py`)

### Tri-Path Zero-Loss Transport Hierarchy
1. **Path A — Lightning Local Burst (`< 15–50 ms`):**
   - WROOM drops `WIFI_PS_MIN_MODEM` → `WIFI_PS_NONE` during `DOOR_OPEN` burst, sends a 1-packet UDP `:5055` LAN broadcast for `server.py`, and hits `192.168.29.10:80/api/v1/event` via direct `WiFiClient` with `TCP_NODELAY` (no `HTTPClient` overhead), then restores `WIFI_PS_MIN_MODEM`.
   - CAM returns `200 OK` immediately while `requestCameraWakeAsync()` (pinned priority-4 FreeRTOS task) initializes OV2640 in parallel.
2. **Path B — Reliable HTTP & ACK Contract:**
   - Include monotonic `seq_id`, `device_id`, `fw_version`, and `time_ms` in every event payload; return `{"ok":true,"seq":...}` ACKs.
   - Server health probe (`_probe_wroom_health`) uses a realistic `2.0 s` timeout matching `MIN_MODEM` beacon sleep RTTs (`~100–300 ms`).
3. **Path C — No-Wi-Fi ESP-NOW Peer Fallback (`<esp_now.h>`):**
   - If router Wi-Fi stays down for `>= 20 s` (`s_fallback_active`), **never** call `WiFi.disconnect(false)` permanently; lock Channel 1, exchange `ERR_NO_WIFI` heartbeats and all door/motion events over ESP-NOW broadcast, and retry `WiFi.begin()` in the background every `30 s` until the router returns.
   - Deliver the `1.2 s` post-door-open QVGA super-compressed JPEG (`quality=42`) to WROOM SPIFFS (`/door_last.jpg`) via HTTP `/api/v1/image` when Wi-Fi is up or chunked 220-byte ESP-NOW (`ESPNOW_IMG_START/DATA/END` + chunk bitmap ACK for retransmission) when Wi-Fi is down.

### Offline Event Ring Buffer (Store-and-Forward)
- Maintain a 64-slot ring buffer in RAM/SPIFFS on WROOM & CAM holding unacknowledged `(seq_id, event_type, src, delta_ms)` entries.
- Replay queued entries in order via `POST /api/v1/events/batch` once Wi-Fi or peer ACK resumes, preventing blind spots across router reboots.

### Heartbeats & Version Compatibility
- `server.py` polls CAM `/status` every `4 s` even during Low-Power Standby (`notifyServerHeartbeat()`), updating real-time temperature/standby state and confirming server liveness.
- Expose `"schema": 1` and `"fw_version"` in `/status` on both ESP32s so the Control Center flags schema drift after partial OTA updates.

---

## 🧠 3. AI / Detection (`server.py`, `vlm_engine.py`, `npu/`)

### Universal Accelerator Hierarchy (`CPU Base → GPU → NPU`)
Implemented via [`npu/accelerator_manager.py`](file:///C:/Users/Adhyatm%20Deharia/Documents/Adhyatm/ESP32-stuff/CAM-stuff/Smart_door_entry_detection/test/npu/accelerator_manager.py) and [`npu/amd_xdna2.py`](file:///C:/Users/Adhyatm%20Deharia/Documents/Adhyatm/ESP32-stuff/CAM-stuff/Smart_door_entry_detection/test/npu/amd_xdna2.py):
1. **CPU (`CPUExecutionProvider`):** Guaranteed server base so Sentinel always boots cleanly on any hardware.
2. **GPU (`CUDAExecutionProvider` / `DmlExecutionProvider`):** Automatically used when available (e.g., AMD Radeon 840M iGPU), bounded by `vlm_engine.py` duty/thermal limits.
3. **NPU (`VitisAIExecutionProvider` / `QNNExecutionProvider` / `OpenVINOExecutionProvider`):** Enabled **only** when the NPU provider and INT8-compatible artifacts/configs (`vaip_config.json`, Ryzen AI environment) are present.

### Multi-Stage Accuracy & False-Positive Pipeline
1. **Frame Quality Gate:** Reject frames with low Laplacian sharpness (motion blur), extreme under/over-exposure, or face boxes smaller than `60×60 px` before running heavy embedding models.
2. **Alignment & Illumination:** Run YuNet 5-point landmarks → canonical `112×112` affine alignment + CLAHE normalization.
3. **Identity & Threshold Bands:** 512-D ArcFace cosine similarity (`Strong >= 0.55`, `Standard >= 0.42`, `Inconclusive` logged separately to prevent mislabeling guests).
4. **Temporal Track Consistency & Suppression:** Require N-of-M matching frames across consecutive tracker IDs before emitting `KNOWN_PERSON` or `UNKNOWN_PERSON`, suppressing 1-frame transient misidentifications and duplicate bursts.

### Backpressure & Resource Limits
- **Non-Blocking Ingestion:** `frame_queue` (`maxsize=4`) drops stale frames when inference lags so live streaming (`:5000/live`) and MP4 door recording never stall.
- **Total-RAM Adaptive Model Selector (`🧠 Models` tab):** Evaluates total physical RAM (compensating for iGPU UMA reservations, e.g., `13.8 GB` visible = `16 GB Tier`) across `<16 GB`, `16 GB`, `24 GB`, and `32 GB+` tiers, guarding `Warning` (2-step pop-up) and `Danger` (4-step pop-up) loads instead of hard-blocking them.
- **Session Recovery:** Wrap `session.run()` so DirectML/VitisAI device-lost errors automatically fall back cleanly to `CPUExecutionProvider`.

---

## 💾 4. Data & Storage (`event_db.py`, `server.py`, SPIFFS/SD)

### SQLite Durability (`data/events.db`)
- **Connection Pragmas:** Always enforce `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;`.
- **Schema Migrations & Indexes:** Version tables via `schema_migrations` for idempotent upgrades; maintain composite indexes on `(timestamp DESC)`, `(event_type, timestamp DESC)`, and `(person_id, timestamp DESC)`.
- **Corruption Self-Healing:** Run `PRAGMA quick_check` on startup; on failure, quarantine the damaged DB (`events.db.corrupt.<ts>`), recreate schema, and backfill from `data/backups/`.

### Glitch-Free Real-Time Video Recorder (`entrance_monitoring/recordings/YYYY/MM/DD/HH-MM-SS.mp4`)
- **Hierarchical Storage:** Automatically organizes recordings into `recordings/<Year>/<Month>/<Day>/<HH-MM-SS>.mp4`.
- **Resolution-Switch Immunity:** Locks `(door_video_w, door_video_h)` on frame 1 and transparently `cv2.resize()`s incoming frames if ESP32-CAM temporarily switches to `QVGA` (such as the `1.2 s` WROOM compressed capture) mid-clip.
- **Wall-Clock Pacing & Finalization:** Uses `mp4v` at `10.0 FPS` with elapsed-time frame pacing (`1.0×` real-time speed regardless of Wi-Fi FPS swings), dual `threading.Timer` + `atexit` `.release()` finalizers, and automatic deletion of 0-frame stubs.

### Retention & Disk-Space Janitor
- Background hourly check (plus pre-recording guard): when free disk falls below `2.0 GB` or retention age/quota exceeds configured limits, prune oldest recordings/snapshots FIFO and record an audit entry.
- On ESP32-CAM SPIFFS (`192 KB` in `partitions_ota_min.csv`), rotate `INFO`/`WARN`/`ERROR` logs at `64 KB` (`/logs.txt` → `/logs.prev.txt`).

---

## 🔔 5. Notifications (`notification_manager`, `server.py`)

### Provider Isolation & Anti-Spam Design
- **Supported Channels:** Webhook, MQTT, Push (`ntfy`), Telegram, and WhatsApp (AiSensy API / Gateway — secrets stored exclusively in backend `data/notification_config.json`, never flashed to ESP32 firmware).
- **Per-Provider Isolation:** Dispatch through an async worker where each provider executes with strict connect/read timeouts inside isolated try/except handlers—one dead broker never stalls Telegram or webhooks.
- **Deduplication & Cooldowns:** Deduplicate rapid repeats using `(event_type, person_id, dedup_key)` cooldown windows; suppress `CAMERA_OFFLINE` alerts whenever `board_online == True` (intentional Low-Power Standby).
- **Retry Backoff & History:** Retry transient `5xx`/network errors with exponential backoff (`2 s → 5 s → 15 s`), persisting final delivery status (`sent`, `retrying`, `failed`) in notification history.

---

## 🌐 6. Dashboard (`templates/index.html`)

### Honest Visual Hierarchy & Low-Friction Controls
- **3-State Hardware Representation:** Keep `#cameraStatusBadge`, the `LIVE STATUS` sidebar, and the `Mini Live Stream` consistent:
  - 🟢 **Live Streaming** (`camera_online: true`)
  - 🟡 **Online • Cam-Sens Off (Low-Power)** (`board_online: true, camera_online: false`)
  - 🔴 **Offline** (`board_online: false` — never cached stale `true` after `/status` failure)
- **Accurate Security Pill:** Compute `ATTENTION REQUIRED` vs. `SYSTEM SECURE` exclusively from **unacknowledged**, **recent (< 15 min)** critical alerts (excluding `CAMERA_OFFLINE` during Low-Power Standby).
- **Live Door-Open Audio Chime:** Unlock Web Audio API on first user click and fire the two-tone chime automatically whenever `door_open_seq` increments.
- **Dedicated Clean Pages:**
  - `🛡️ Overview` | `📹 Live Camera` | `📋 Events` | `👤 People` | `🔔 Alerts` | `🔌 Devices` | `📊 Analytics` | `⚙️ Settings` (1 s live CPU / GPU / NPU hardware telemetry + single Security Hardening card) | `🧠 Models` (card tabs + RAM warning/danger pop-ups) | `🧪 Testing` (timestamped log console + mini stream, no dangerous auto-disconnect buttons).

---

## 🐳 7. Docker (`Dockerfile`, `docker-compose.yml`)

- **Reproducible Build & Runtime:** Single `docker compose up -d` launches `:5000` (Waitress WSGI production Sentinel server) and `:5001` (Control Center) with Python ML dependencies and PlatformIO + ESP32 toolchains pre-cached inside the image.
- **Persistent Named Volumes:** Persist `/app/data`, `/app/entrance_monitoring`, `/app/models`, and `/app/workspace` across image upgrades without storing `.bin` artifacts or user credentials inside Git images.
- **Health Checks & Safe Upgrades:** `HEALTHCHECK` probing `/api/v1/system/healthz`; secrets passed strictly via `.env` / local volume mounts; clean container stop signals (`SIGTERM` → `atexit` flushes open `.mp4` writers and closes SQLite WAL cleanly).

---

## 🛠️ 8. Developer / Control Center (`control_center.py`, `cc_managers.py`)

- **Isolated Workspaces:** Separate `release/` (verified baseline), `workspace/` (editable source), and `artifacts/` (`.bin` outputs + build manifests) without polluting live server runtime.
- **Safe Editor & Build Manager:** Whitelist-restricted path editor with revert snapshots, streaming PlatformIO build/clean/audit logs (`esp32cam` & `esp32wroom` verified against `partitions_ota_min.csv` 2-OTA layout, `1,920 KB × 2 + 192 KB SPIFFS`).
- **Authoritative Upload & Rollback:** Always delegate OTA/USB uploads to [`scripts/ota_upload.py`](file:///C:/Users/Adhyatm%20Deharia/Documents/Adhyatm/ESP32-stuff/CAM-stuff/Smart_door_entry_detection/test/scripts/ota_upload.py) and keep the previous verified `.bin` per target for instant rollback.

---

## 🔐 9. Security (`security_hardening.py`, `server.py`)

- **Authentication & Access Control:** Password/Admin PIN protection (`/api/v1/security/pin`, `/api/v1/security/login`) with salted hashing, session cookies, optional localhost lock, and 1-click rotatable `X-API-Key` for programmatic access.
- **Abuse & Brute-Force Protection:** Sliding-window rate limiter (`40` mutating / `160` read requests per `10 s`) + progressive login lockout generating a `SYSTEM_FAILURE` critical alert.
- **Safe Boundaries & Input Validation:** Zero arbitrary shell execution (`netsh` AP switching gated behind explicit UI confirmation + whitelisted SSID), strict `os.path.realpath` containment checks on all file/recording APIs, zero secrets in firmware repo, and full SQLite `audit_log` trail.

---

## 🧪 10. Reliability & "Ugly Situations" Chaos Matrix

| Failure / Edge Scenario | Automatic Detection Mechanism | Recovery & Safeguard Behavior | User-Visible Outcome |
| :--- | :--- | :--- | :--- |
| **Wi-Fi router dies or restarts** | `WiFi.status() != WL_CONNECTED` for `>= 20 s` (`wifi_manager.cpp`) | Enters Ch 1 ESP-NOW fallback + CAM SoftAP `Sentinel-CAM`; retries `WiFi.begin()` every `30 s` without disabling STA | Door events & 1.2 s images keep flowing; connect to `192.168.4.1` mini dashboard until router returns |
| **WROOM battery dies or tampered off door** | 16 consecutive failed `/events` polls (~8 s) after being online (`server.py`) | Fires once per outage: emits `WROOM_OFFLINE` critical alert, wakes CAM, records 10 s safety `.mp4`, restores standby | Red tamper alert + 10 s safety video saved automatically |
| **ESP32-CAM sensor lockup (`0x103` / DMA stall)** | `s_camera_mutex` serialization + 5 consecutive `NULL` frames | Full `250 ms` `PWDN HIGH→LOW` hardware reset & re-init | Self-heals without manual power cycle |
| **Door opens while CAM is in Low-Power Standby** | `< 1 ms` Reed ISR → direct `TCP_NODELAY` + UDP `:5055` + async `cam_wake` | CAM wakes in parallel, server arms `.mp4` in `< 150 ms`, 1.2 s QVGA saved to WROOM SPIFFS, 8 s post-close auto-standby | Instant recording + zero idle power waste when closed |
| **Mid-recording resolution switch or server stop** | `img.shape != (door_video_w, door_video_h)` or timer/exit signal | Auto `cv2.resize()` + `threading.Timer` & `atexit` `.release()` | 100% playable `1.0×` `.mp4` under `YYYY/MM/DD/HH-MM-SS.mp4` |
| **AI server overload or missing NPU/model** | `frame_queue.full()` or missing `.onnx` / EP error | Drops stale frames; falls back `NPU → GPU → CPU`; missing file blocked cleanly with message after guard flow | Video recording & UI never freeze |
| **Database locked/corrupt or MQTT/Telegram down** | SQLite `busy_timeout=5000` / `quick_check`; provider timeout | Isolated per-provider retry backoff; DB auto-quarantine + backup restore | Rest of system stays online cleanly |

---

## 🧹 11. Code Quality & Modularization Roadmap

Because [server.py](file:///C:/Users/Adhyatm%20Deharia/Documents/Adhyatm%20Deharia/ESP32-stuff/CAM-stuff/Smart_door_entry_detection/test/server.py) has grown past ~4,000 lines, future cleanup should extract responsibilities cleanly into packages (mirroring how [`npu/`](file:///C:/Users/Adhyatm%20Deharia/Documents/Adhyatm/ESP32-stuff/CAM-stuff/Smart_door_entry_detection/test/npu/README.md) was already separated) while keeping `python server.py` as the single entrypoint:

```text
test/
├── server.py                  # Lean WSGI entrypoint + Flask app factory + background thread wiring
├── core/
│   └── config.py              # Single source of truth for env vars, IPs, ports, thresholds
├── camera/
│   ├── stream_consumer.py     # mjpeg_consumer_thread + standby /status poller
│   └── recorder.py            # DoorVideoRecorder (YYYY/MM/DD MP4, 1.0x pacing, resize lock, atexit)
├── ai/
│   └── face_pipeline.py       # Quality gate, YuNet align, ArcFace match, temporal voting
├── npu/                       # Already extracted! Hardware accelerator priority & AMD XDNA 2 SDK helper
├── routes/                    # Flask Blueprints: camera, events, models, testing, security, devices
├── event_db.py                # SQLite WAL store + migrations + retention janitor
├── vlm_engine.py              # Bounded-duty object detector
├── security_hardening.py      # Auth, PIN, API key, rate limiter, audit trail
├── control_center.py          # :5001 dev/build/OTA manager
└── firmware/esp32-cam/        # Clean hardware HAL + minimal 2-OTA partition layout
```

**Code Hygiene Rules:**
- Zero silent `except Exception: pass` when managing online/offline or lock flags.
- Centralize repeated hardware/HTTP calls into shared helpers rather than copy-pasted blocks.
- Keep structured `[timestamp][LEVEL][TAG]` logs concise and actionable (`INFO`, `WARN`, `ERROR`).

---

## 🚀 13. Sentinel Omega High-Density Additions (Implemented)

- **Wi-Fi, SoftAP (`192.168.4.1`), ESP-NOW & RF Chaos Lab (`#page-testing` + `wifi_manager.cpp` + `server.py`):**
  - On-demand `wifi_sim_mode` (`0` Normal STA, `1` Forced Router Blackout + AP + ESP-NOW Ch 1, `2` Forced Dual AP+STA), `espnow_sim_burst`, `wifi_tx_power_sweep` (`2..20 dBm`), `wifi_proto_switch` (`802.11b/bg/bgn` throughput benchmark), `wroom_blackout_sim`, and `netsh` AP/Home-Wi-Fi switcher reading credentials dynamically from `wifi_secrets.h` / `secrets.h`.
  - 14-field live RF diagnostics in `/status` (`ip`, `ap_ip`, `sta_connected`, `ap_active`, `ap_ssid`, `ap_clients`, `espnow_fallback`, `wifi_sim_mode`, `rssi`, `signal_quality_pct`, `wifi_ch`, `tx_pwr`, `wifi_proto`, `reconnect_count`, `last_server_hb_age_ms`).
- **Cooling Fan, Closed-Loop PID Thermal Governor & Power Mega-Card (`#page-settings` + `cooling.cpp`):**
  - 3 unified modes in `evaluateCooling()`: Stepped Thermal Curve (`0`), Closed-Loop PID Target Temp (`1`, anti-windup), and Manual Fixed PWM Override (`2`), plus configurable PWM frequency (`100–40000 Hz`), kickstart pulse (`0–1000 ms`), thermal runaway guard (`>78°C`), and instant `0% / 50% / 60% / 75% / 100% / Auto` controls.
- **Pre-Trigger Circular RAM DVR + Behavioral Heuristics + SHA-256 Evidence Ledger (`server.py` + `event_db.py`):**
  - 80-frame circular RAM pre-trigger buffer automatically prepended when a `DOOR_OPEN` `.mp4` starts.
  - Real-time loitering (`>15s` dwell) and tailgating (`>=2` people during active door open) detection inside `process_frame()`.
  - Tamper-evident `hash_ledger` SHA-256 chain in `data/events.db` with `verify_ledger_integrity()` exposed at `/api/v1/ledger/verify` and the Testing Chaos Lab.

## 🚀 12. Final Polish Checklist Before Public / Daily Release

- [ ] Verify end-to-end "Ugly Situation" chaos suite from the **`🧪 Testing`** tab (`ping_nodes`, `door_open`, `cam_snapshot`, `door_closed`, `wroom_saved_image`, `cam_spiffs_logs`).
- [ ] Run one intentional WROOM disconnect test to confirm the single 10 s safety `.mp4` and alert trigger.
- [ ] Verify Control Center (`:5001`) builds cleanly for both `esp32cam` and `esp32wroom` using `partitions_ota_min.csv` inside Docker.
- [ ] Quantize `yolo11s.onnx` and `insightface/recognition/model.onnx` to **INT8** and launch inside the Ryzen AI `1.9.0` venv when ready to shift continuous 24/7 inference from the 840M iGPU to the **XDNA 2 NPU** (~10–13 W draw).
- [ ] Audit `git status` / `.gitignore` to ensure `secrets.h`, `data/`, `entrance_monitoring/`, and `.env` are never committed.
