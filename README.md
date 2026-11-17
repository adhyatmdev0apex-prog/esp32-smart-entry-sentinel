# ESP32 Smart Entry Sentinel

A local smart-entry system combining an ESP32-CAM, an optional ESP32-WROOM sensor node, and a Python dashboard. The microcontrollers handle camera and sensor work; the computer runs face recognition, optional object detection, event history, and the web interface.

> **New here?** Follow [Quick start](#quick-start) in order. You can run the dashboard without hardware, then connect and verify the camera before enabling the other features.

## Contents

- [What the project does](#what-the-project-does)
- [Quick start](#quick-start)
- [Configure login and protect secrets](#configure-login-and-protect-secrets)
- [Run with Docker](#run-with-docker)
- [Connect and verify the ESP32-CAM](#connect-and-verify-the-esp32-cam)
- [Build and upload firmware](#build-and-upload-firmware)
- [Use the dashboard](#use-the-dashboard)
- [Feature and source map](#feature-and-source-map)
- [API reference](#api-reference)
- [Models and acceleration](#models-and-acceleration)
- [Data, backups, and reset](#data-backups-and-reset)
- [Troubleshooting](#troubleshooting)
- [Validation tools](#validation-tools)

## What the project does

```text
ESP32-WROOM (optional)                 ESP32-CAM
  reed / PIR / buzzer  -- Wi-Fi/ESP-NOW --> camera / snapshots / MJPEG
          |                                     |
          +-------------- LAN ------------------+
                                                |
                                                v
                                    Python server (:5000)
                                    face recognition, optional
                                    object detection, events, UI
                                                |
                                                v
                                  Control Center (:5001)
```

| Part | What it does | Default address |
| --- | --- | --- |
| Python dashboard (`server.py`) | Live view, camera controls, face recognition, profiles, events, analytics, notifications | `http://localhost:5000` |
| Control Center (`control_center.py`) | Firmware build/upload, config audit, workspace editor, GitHub release/rollback tools | `http://localhost:5001` |
| ESP32-CAM | OV2640 camera, control/status API, still JPEG and MJPEG stream | HTTP `:80`, stream `:81` |
| ESP32-WROOM (optional) | Door reed switch, PIR motion, buzzer, event relay | HTTP `:80` |

The default network addresses in the example configuration are CAM `192.168.29.10` and WROOM `192.168.29.20`. Your computer and devices must be able to reach each other on the same LAN; guest Wi-Fi or client isolation can block device-to-device traffic.

## Quick start

### 1. Start the dashboard

Use Python 3.11, and run commands from the repository root. A virtual environment keeps this project's packages separate from other Python projects.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install onnxruntime huggingface_hub zeroconf psutil
```

On Linux/macOS, activate the environment with:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

`requirements.txt` includes legacy `dlib` / `face-recognition` dependencies. They are optional compatibility imports in the current server. If installing them fails on your computer, use the core server packages instead:

```powershell
python -m pip install Flask numpy opencv-python pillow waitress onnxruntime huggingface_hub zeroconf psutil
```

Start each service in its own terminal:

```powershell
python server.py
```

```powershell
python control_center.py
```

Wait for the startup messages, then open:

- Dashboard: <http://localhost:5000>
- Control Center: <http://localhost:5001>

The server downloads the YuNet face detector and default InsightFace `buffalo_m` recognition model on first startup if they are missing. Allow outbound network access for that first run. Object detection needs a separate ONNX model; see [Models and acceleration](#models-and-acceleration).

### 2. Add hardware after the server is running

You can use the dashboard without ESP32 boards, but live camera and device status need the hardware. Complete [Configure login and protect secrets](#configure-login-and-protect-secrets), then [Build and upload firmware](#build-and-upload-firmware). Verify one still JPEG successfully before troubleshooting the live stream.

## Configure login and protect secrets

The login page reads credentials from `firmware/esp32-cam/include/secrets.h`. The example file contains Wi-Fi/network placeholders; add your own login defines there as well:

```cpp
#define SECRET_ADMIN_USER     "choose-an-admin-name"
#define SECRET_ADMIN_PASSWORD "replace-with-a-long-unique-password"
#define SECRET_VIEW_USER      "choose-a-viewer-name"
#define SECRET_VIEW_PASSWORD  "replace-with-another-unique-password"
```

Copy `firmware/esp32-cam/include/secrets.example.h` to `firmware/esp32-cam/include/secrets.h` if you need a starting point, then set the SSID, password, static IP, gateway, and custom-MAC options to match your network. The header uses static-IP and custom-MAC options by default; disable either option if it conflicts with your router or another device.

**Keep real credentials private.** This checkout's `secrets.h` is present in Git's tracked-file list, so do not assume `.gitignore` makes it private. Before publishing or sharing this repository, remove secrets from tracked files and Git history, rotate any credentials that were exposed, and verify your own ignore rules. Do not paste credentials into issues, screenshots, logs, or this README.

The web dashboard and Control Center share these login credentials, but the Control Center requires the admin role for remote access. The dashboard's PIN/API-key controls are separate additional protections. By default, loopback access is trusted unless the localhost lock is enabled. Do not expose either web port directly to the public internet.

For Docker, `WIFI_SSID`, `WIFI_PASSWORD`, `ADMIN_PASSWORD`, and `USER_PASSWORD` in `.env` can override matching values at container startup. These are optional; `.env` is not automatically loaded by direct `python server.py` commands. Set shell environment variables yourself when running Python directly.

## Run with Docker

Docker Compose runs both Python services in one container and persists application data in named volumes.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
docker compose up --build
```

Open <http://localhost:5000> and <http://localhost:5001>. Stop the services with `docker compose down`; add `-v` only if you intentionally want to delete the persistent Docker volumes and their data.

**Current Docker dependency note:** `server.py` imports `huggingface_hub` at startup, but the current `Dockerfile` and `requirements.txt` do not install it. Until that dependency is added to the image, the container can fail during startup with `ModuleNotFoundError: huggingface_hub`. For a local run, the extra `pip install` in [Quick start](#quick-start) provides it; for Docker, add `huggingface_hub` to the Docker image's pip install step and rebuild.

The environment file supports:

| Variable | Default | Used for |
| --- | --- | --- |
| `PORT` | `5000` | Dashboard port |
| `CONTROL_CENTER_PORT` | `5001` | Control Center port |
| `MDNS_HOST` | `doorcam` | Dashboard mDNS name (`http://doorcam.local:5000` where supported) |
| `ESP32_CAM_IP` | `192.168.29.10` | Control Center's CAM target |
| `ESP32_WROOM_IP` | `192.168.29.20` | Control Center's WROOM target |
| `GITHUB_REPO` | project default | Control Center release source (`owner/repo`) |
| `WIFI_SSID`, `WIFI_PASSWORD` | empty | Optional firmware Wi-Fi credential injection in Docker |
| `ADMIN_PASSWORD`, `USER_PASSWORD` | empty | Optional replacement for matching login defines in Docker |

When running locally without Docker, the Python server's camera discovery uses its candidate IPs in `server.py`; the WROOM health/reboot target is configured there too. If you use a different LAN, update the firmware and the corresponding server/Control Center addresses.

`start.bat` (Windows) and `start.sh` (Linux/macOS) start Docker Compose when Docker is available, otherwise they launch both Python services. For direct local development, the two commands in [Quick start](#quick-start) make it easier to see each service's logs.

## Connect and verify the ESP32-CAM

The camera sensor intentionally boots in low-power standby. A running HTTP server does not mean the sensor or video stream is active.

### 1. Check the device and wake the sensor

On Windows, use `curl.exe` (rather than PowerShell's `curl` alias):

```powershell
curl.exe -f http://192.168.29.10/ping
curl.exe -f http://192.168.29.10/status
curl.exe -f "http://192.168.29.10/api/v1/camera/power?state=on"
```

The status response should show the device online; after waking, look for `sensor_active: true` and `ready: true`. If those are false, check serial output and the camera wiring/power before continuing.

### 2. Test a still image first

```powershell
curl.exe -f -o capture.jpg http://192.168.29.10/capture
python -c "from pathlib import Path; b=Path('capture.jpg').read_bytes(); assert b[:2] == bytes.fromhex('ffd8') and b[-2:] == bytes.fromhex('ffd9'), 'Not a complete JPEG'; print(f'Valid JPEG: {len(b)} bytes')"
```

Do not treat an HTTP response alone as proof that capture works: the downloaded file must have both JPEG markers. If this fails, inspect `/status` and serial logs before testing the stream.

### 3. Open the MJPEG stream

Once `/capture` returns a valid JPEG, open <http://192.168.29.10:81/stream> or use the dashboard at <http://localhost:5000>. To test the device URL directly, close other live-view clients first; the camera stream server handles clients sequentially.

For a stopped stream, check that the sensor remains active and ready, the computer can reach TCP port 81, and the serial log reports the stream server listening. A sleeping sensor or a blocked port can make the browser wait without receiving frames.

## Build and upload firmware

### Requirements and configuration

- Install PlatformIO Core (`pio`) or the PlatformIO VS Code extension.
- Connect the board using a reliable USB-to-serial adapter/programmer and stable power.
- Configure credentials and network options in `firmware/esp32-cam/include/secrets.h`.
- Review `firmware/esp32-cam/include/configs.h` before flashing. The default mode is `SYSTEM_MODE_WROOM_CAM`; the PlatformIO environments are `esp32cam` and `esp32wroom`.

### Build

Run from the repository root:

```powershell
pio run -d firmware/esp32-cam -e esp32cam
pio run -d firmware/esp32-cam -e esp32wroom
```

Build the environment for the board you intend to flash. A successful build does not upload firmware.

### Upload over USB

Replace `COM6` with the port shown for your adapter:

```powershell
pio run -d firmware/esp32-cam -e esp32cam -t upload --upload-port COM6
```

For the WROOM board, use `-e esp32wroom`. After upload, open the serial monitor at 115200 baud and confirm the startup logs, Wi-Fi address, and camera initialization before testing HTTP endpoints.

### Upload over Wi-Fi

The firmware enables ArduinoOTA on port 3232. The helper can build and upload to the default CAM target:

```powershell
python scripts/ota_upload.py --build --target cam
```

Options include `--target wroom` or `--target both`, `--ip <address>` for one target, `--file <firmware.bin>` to select a built image, and `--port 3232` to change the OTA port. OTA requires the board and computer to be on a reachable LAN; local firewall rules may need to allow the transfer. Confirm the device reboots and `/status` responds afterward. Use USB serial if OTA cannot be verified.

The PlatformIO partition table is `firmware/esp32-cam/partitions_ota_min.csv` and defines two OTA application slots plus SPIFFS.

### Configure board modes and pins

Edit `firmware/esp32-cam/include/configs.h` for operating mode, sensor enables, camera resolution/quality, cooling, optional SD, local face finding, and optional server analysis. Hardware pin definitions are in `firmware/esp32-cam/boards/`.

| Board/profile | Default hardware |
| --- | --- |
| AI-Thinker ESP32-CAM | OV2640; flash LED GPIO 4; cooling fan GPIO 13; camera pin map in `ai_thinker_esp32_cam.h` |
| ESP32-WROOM DevKit | reed GPIO 27; PIR GPIO 14; buzzer GPIO 26 |

On the WROOM board, reed and PIR are enabled by default; the buzzer is enabled but may not be attached. On the CAM profile they are disabled by default. The CAM's SD feature is off by default. If enabling SD, note that its bus reserves GPIO 2, 4, 12, 13, 14, and 15; do not assume those pins remain available for other hardware.

The default CAM frame size is SVGA, JPEG quality is 12 (lower means higher quality), and XCLK is 20 MHz. Supported camera controls include:

- `framesize`: QVGA, CIF, VGA, SVGA, XGA, HD, SXGA, or UXGA.
- `quality`: 10–63; lower values mean higher JPEG quality.
- `brightness`, `contrast`, `saturation`: -2 to 2.
- `special_effect`: 0–6; `vflip`, `hmirror`, `awb`, and `aec`: 0 or 1.
- `flash`: 0–255 (GPIO 4); it is off by default.

The device `/control` API also exposes Wi-Fi transmit power/protocol and cooling parameters. The dashboard persists a smaller set of camera defaults and confirms changes by reading the device status back.

## Use the dashboard

The dashboard's navigation is organized around these tasks:

| Area | What you can do |
| --- | --- |
| Overview / identities | Review people, recognition status, recent detections, and identity profiles |
| Camera | Watch live video; enable/disable face recognition and object detection; toggle overlays; adjust camera controls and flash; wake/standby the sensor |
| Events | Search/filter stored recognition and hardware events; inspect the audit log |
| People | Enroll identities from images, add samples, label profiles, set identity metadata, inspect quality and samples, and review detections |
| Duplicate review | Compare possible duplicate profiles, preview a merge, then confirm a merge |
| Devices | Check CAM, WROOM, AI service, and event-store health; trigger device reboot or stream reconnect |
| Analytics | Review hourly/daily event summaries and top visitors |
| Alerts | Configure event-to-provider rules, test delivery, and view recent alerts |
| Settings | Save default camera settings, manage security PIN/API key, and select available AI models |
| Control Center | Build/upload firmware, audit configuration, edit whitelisted workspace files, check releases, install, or roll back |

**Important actions:** Deleting a profile or using the dashboard's full reset can remove saved identity/sample data. Review the confirmation/preview first and keep a backup if the data matters. The flash control operates the ESP32-CAM's high-power LED; it is off by default.

## Feature and source map

Use this map to find implementation details without searching the whole repository:

| Source | Responsibility |
| --- | --- |
| `server.py` | Flask routes, camera stream consumer, face detection/recognition, profile/sample lifecycle, recordings, camera controls, device health, event ingestion, notifications, model selection, dashboard startup |
| `vlm_engine.py` | ONNX object detector, model load/reload/status, object boxes, optional image descriptions |
| `event_db.py` | SQLite schema, recognition/system events, audit trail, search, analytics, clearing event history |
| `security_hardening.py` | Login credential lookup, PIN/API key, access control, rate limiting, input/path validation, security headers, notification-secret masking |
| `notifications/` | Persistent provider settings, event queue/deduplication/cooldowns/retries, dashboard alerts, webhook, MQTT, Telegram, push, and WhatsApp provider adapters |
| `control_center.py` | Admin login and Control Center routes |
| `cc_managers.py` | Safe workspace editor, GitHub release/backup/rollback, PlatformIO build/audit/artifacts, OTA/serial upload |
| `templates/` | Dashboard (`index.html`), login (`login.html`), and Control Center (`control_center.html`) interfaces |
| `firmware/esp32-cam/src/main.cpp` | Device startup order and main service loop |
| `firmware/esp32-cam/src/camera/` | Camera lifecycle, standby/wake, shared capture lock, stream server, adaptive cooling |
| `firmware/esp32-cam/src/network/` | Wi-Fi, mDNS, ArduinoOTA, device HTTP API |
| `firmware/esp32-cam/src/sensors/` | Reed switch, PIR, buzzer |
| `firmware/esp32-cam/src/core/` | Device event system, logging, reboot/system diagnostics |
| `firmware/esp32-cam/src/communication/wroom_cam_link.cpp` | WROOM-to-CAM event relay over Wi-Fi, with ESP-NOW fallback; event-triggered camera actions |
| `firmware/esp32-cam/src/storage/`, `src/detection/`, `src/ai/` | Optional SD storage, lightweight face detection, and local AI-server client |
| `scripts/` | Docker startup, model download helper, OTA upload, hardware capture/stream validation, security and notification tests |
| `docs/` | Project plan and roadmap; these are planning documents, not a feature-by-feature guarantee |

The two firmware builds share source but compile for different board profiles. Read `configs.h` and the selected board profile before changing pins or enabling optional hardware.

## API reference

### Python dashboard — port 5000

| Route | Methods | Purpose |
| --- | --- | --- |
| `/` | GET | Dashboard |
| `/live` | GET | Annotated MJPEG feed from the server |
| `/stream/status` | GET | Stream and recognition telemetry |
| `/stream/reconnect` | POST | Restart the server-side stream connection |
| `/api/recognition/toggle`, `/api/v1/vlm/toggle`, `/api/toggle_overlay` | POST | Toggle face recognition, object detection, or overlays |
| `/api/persons` | GET | List identity profiles |
| `/api/person/enroll`, `/api/person/<id>/upload-samples` | POST | Enroll a person or add images |
| `/api/person/<id>/label`, `/api/person/<id>/identity` | POST | Edit profile label or identity metadata |
| `/api/person/<id>/samples` | GET | Page through stored face samples |
| `/api/person/<id>/sample/<filename>` | DELETE | Delete one sample |
| `/api/person/<id>/sample/<filename>/toggle-gallery` | POST | Include/exclude a sample from the matching gallery |
| `/api/person/<id>/samples/batch` | POST | Batch sample actions |
| `/api/duplicates`, `/api/merge/preview`, `/api/merge` | GET/POST | Find duplicates, preview a merge, and confirm it |
| `/api/person/<id>/delete-preview`, `/api/person/<id>/delete` | GET/POST | Preview and delete a profile |
| `/api/detections/<id>` | GET | Detection history for one person |
| `/api/camera/settings` | GET/POST | Read camera status or apply a supported sensor/flash setting with read-back |
| `/api/camera/defaults` | GET/POST | Get/save persistent defaults and apply them to the camera |
| `/api/camera/sensor` | GET/POST | Read or wake/standby the camera sensor |
| `/api/v1/analyze`, `/upload` | POST | Analyze an uploaded image |
| `/api/v1/vlm/status`, `/api/v1/vlm/describe` | GET/POST | Object detector status or an on-demand image description |
| `/api/v1/models/catalog`, `/api/v1/models/select` | GET/POST | List/select installed model files |
| `/api/v1/devices/health` | GET | CAM, WROOM, AI service, database, and notification health |
| `/api/v1/event` | POST | Ingest a hardware/system event |
| `/api/v1/events/db`, `/api/v1/audit`, `/api/v1/analytics` | GET | Query event history, audit history, and aggregates |
| `/api/v1/notifications/status`, `/api/v1/notifications/config`, `/api/v1/notifications/test` | GET/POST | Notification status, provider/rule settings, and test delivery |
| `/api/v1/system/healthz` | GET | Server thread, resource, model, and security diagnostics |
| `/api/v1/security/status`, `/api/v1/security/pin`, `/api/v1/security/login`, `/api/v1/security/logout`, `/api/v1/security/rotate-key` | GET/POST | Security status, PIN/access settings, session, and API-key management |
| `/api/system/reboot_cam`, `/api/system/reboot_wroom`, `/api/system/full_reboot` | POST | Reboot one/both devices; full reboot also reloads AI models |
| `/reset` | POST | Clear identity and event data; use with care |
| `/login`, `/logout` | GET/POST | Browser login/logout |

### ESP32 device — port 80

| Route | Methods | Purpose |
| --- | --- | --- |
| `/ping` | GET | Basic reachability check |
| `/status`, `/api/v1/status` | GET | Device, Wi-Fi, camera, stream, sensor, and cooling diagnostics |
| `/events`, `/api/v1/events` | GET | Recent device event buffer |
| `/control?var=<name>&val=<integer>` | GET | Set supported camera, flash, cooling, or Wi-Fi controls |
| `/capture`, `/api/v1/snapshot` | GET | Return a JPEG; capture wakes the sensor if needed |
| `/api/v1/camera/power?state=on|off` | GET/POST | Wake or put the camera sensor into standby |
| `/api/v1/event`, `/event` | POST | Receive a peer event; CAM reacts to door events |
| `/logs`, `/api/v1/logs` | GET | Read device logs |
| `/reboot` | GET | Reboot the board |

The ESP32-CAM serves MJPEG separately at `http://<camera-ip>:81/stream`. Device API endpoints are intended for a trusted local network; they are not a substitute for an internet-facing authenticated API.

### Control Center — port 5001

The browser UI is at `/`. Its `/api/cc/` endpoints provide health/config, release check/install/rollback, workspace tree/file/revert, build status/audit/start, and upload status/start. Upload mode is `ota` or `serial`; the configured target is the ESP IP or serial port. Remote Control Center access requires admin login.

## Models and acceleration

| Task | Runtime default | Notes |
| --- | --- | --- |
| Face detection | OpenCV YuNet: `models/face_detection_yunet_2023mar.onnx` | Downloaded by `server.py` if absent |
| Face recognition | InsightFace `buffalo_m` 512-D: `models/insightface_m/recognition/model.onnx` | Downloaded by `server.py` if absent; new unknown profiles require repeated, acceptable-quality samples |
| Object detection | `models/yolo11s.onnx` if present; otherwise `models/yolov8n.onnx` | Not automatically downloaded by the server; model must be present and compatible |

ONNX Runtime uses available execution providers with CPU fallback. On Windows, DirectML can be installed instead of the standard ONNX Runtime package:

```powershell
python -m pip uninstall -y onnxruntime
python -m pip install onnxruntime-directml
```

The actual active provider is reported in server/model diagnostics. GPU/NPU acceleration is optional; the application can run on CPU, but speed depends on computer and model size. The Control Center model catalog only lists files available on disk. Its accelerator summary also imports the local `npu` helper package; include that package when distributing the catalog feature.

`scripts/download_models.sh` is a helper for Unix-like shells, not required for first startup. Its recognition-model download target currently differs from the `buffalo_m` model path used by `server.py`; rely on the server's own YuNet/`buffalo_m` download or verify model paths before using the helper. Exporting a YOLO ONNX model via that script also requires additional Ultralytics/PyTorch packages.

## Data, backups, and reset

| Location | Contents |
| --- | --- |
| `data/events.db` | SQLite recognition/system event history and audit entries |
| `data/identity_profiles.pkl`, `face_encodings.pkl` | Face embeddings and profile matching data |
| `data/person_labels.json`, `detections.json` | Labels and detection history (legacy data is migrated where supported) |
| `data/camera_defaults.json` | Persisted dashboard camera defaults |
| `data/notification_config.json`, `security_config.json` | Provider settings, PIN hashes, API key, and server security state |
| `entrance_monitoring/` | Face samples, full images, unrecognized images, and backups |
| Docker named volumes | Persisted data, monitoring images, models, workspace, releases, builds, and config |

Back up `data/` and `entrance_monitoring/` while the server is stopped. For Docker, use a volume-aware backup process before removing volumes. The dashboard reset endpoint is destructive; it is not a substitute for a backup. Never commit generated images, databases, model files, or credentials unintentionally.

## Troubleshooting

| Symptom | Check in this order |
| --- | --- |
| Dashboard opens, but camera says offline | Confirm the CAM IP in `/status`, verify computer/device are on the same non-isolated LAN, then open `/ping` and `/status` directly |
| `/capture` returns an error or non-JPEG | Wake the sensor using `/api/v1/camera/power?state=on`; check `sensor_active`/`ready` in `/status`, serial logs, camera ribbon orientation, stable 5 V power, and the sensor connector |
| `/stream` shows no bytes | First get a complete JPEG from `/capture`; then ensure the camera remains awake, no other client is holding the sequential stream connection, and TCP port 81 is reachable |
| ESP32 cannot join Wi-Fi | Check SSID/password and static IP/gateway/subnet in `secrets.h`; ensure the custom MAC is unique and accepted by the router; monitor serial output |
| Settings appear saved but hardware does not change | Check the camera is online and the setting is supported; `/api/camera/settings` reports errors if device read-back does not match |
| Face recognition does not start | Look at server startup for model download/import errors; allow internet on first launch, verify the `buffalo_m` file exists, and check `/api/v1/system/healthz` |
| Object detection is unavailable | Ensure a supported YOLO ONNX file exists in `models/`; the server does not fetch it automatically |
| WROOM events do not reach the dashboard | Confirm the WROOM is online and sensor pins are wired correctly; check CAM/WROOM addresses and allow LAN HTTP plus server UDP port 5055 through the computer firewall |
| Notification test fails | Check provider enablement, credentials, URL/broker reachability, event rules, and the provider error in `/api/v1/notifications/status` |
| Firmware build succeeds but upload fails | Select the correct environment/serial port; for OTA confirm device IP, port 3232 reachability, and host firewall; verify reboot and `/status` after upload |
| Remote login or Control Center access is denied | Confirm login macros in `secrets.h`; remote Control Center access requires the admin role. Loopback trust does not apply when accessing by LAN IP |
| Dashboard mutations return 401/403 | Log in with an admin account; a viewer account can browse but cannot perform admin mutations |

For camera failures, capture the serial log around startup and `/capture`. Do not change multiple camera settings at once; a physical ribbon/connector or power issue cannot be diagnosed from the browser alone.

## Validation tools

- `python scripts/test_security.py` — local security checks using temporary test configuration.
- `python scripts/test_notifications.py` — notification queue, retry, deduplication, and local mock-provider checks.
- `python scripts/test_capture.py` — camera still-image check; currently targets `192.168.29.10`.
- `python scripts/run_full_validation.py` — hardware capture/stream/reconnect exercise; currently targets `192.168.29.10`, runs a 60-second stream test, and has a 240 MHz status assertion. Review those assumptions and the camera address before running it on another board.
- `pio run -d firmware/esp32-cam -e esp32cam` and `... -e esp32wroom` — firmware build checks.

Hardware tests contact the configured device and may save a test JPEG in the current working directory. Run them only when you intend to exercise that camera.

## Repository layout

```text
.
├── server.py, control_center.py       # Python services
├── cc_managers.py, security_hardening.py, event_db.py
├── vlm_engine.py                      # ONNX object detection
├── notifications/                     # Notification providers and queue
├── templates/                         # Dashboard, login, Control Center
├── firmware/esp32-cam/
│   ├── platformio.ini                  # esp32cam and esp32wroom environments
│   ├── boards/                         # Board pin/capability profiles
│   ├── include/                        # Configuration, headers, secret template
│   ├── src/                            # Firmware modules
│   └── legacy/                         # Earlier standalone streamer sketch
├── scripts/                            # Startup, OTA, model, and validation helpers
├── models/                             # ONNX model files
├── data/, entrance_monitoring/         # Runtime state and image gallery
└── docs/                               # Design plan and roadmap
```

`firmware/esp32-cam/backup_baseline/`, `backup_recovery/`, `legacy/`, and root `server-orignal.py` are historical/reference copies, not the active PlatformIO or Python startup paths. For a code change, start with the responsible module in [Feature and source map](#feature-and-source-map), then check its callers and relevant validation tool. `docs/plan.md` and `docs/ROADMAP.md` describe intended architecture and planned work; verify current behavior against source and the running device.
