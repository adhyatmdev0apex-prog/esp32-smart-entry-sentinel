# ESP32 Smart Entry Sentinel

A local smart-entry system built around ESP32 hardware and a Python dashboard. The ESP32-CAM provides camera control and MJPEG streaming; the Python server handles face recognition, object detection, event storage, and the web interface. An optional ESP32-WROOM node reports door and motion events.

## Features

- Live camera preview and camera controls.
- Face detection with OpenCV YuNet and face embeddings with the InsightFace `buffalo_m` ONNX model.
- Optional object detection using `models/yolo11s.onnx` when present, otherwise `models/yolov8n.onnx`.
- Door/motion event tracking, identity profiles, audit records, and notifications.
- A separate Control Center for firmware build/upload workflows and project management.
- ESP32-CAM and ESP32-WROOM PlatformIO build environments.

## Architecture

| Component | Purpose | Default address |
| --- | --- | --- |
| `server.py` | Dashboard, camera-stream consumer, recognition, events, and API | `http://localhost:5000` |
| `control_center.py` | Administrative Control Center | `http://localhost:5001` |
| ESP32-CAM | Camera control API and MJPEG stream | HTTP `:80`, stream `:81` |
| ESP32-WROOM | Optional door/motion sensor node | Configured in firmware |

The dashboard's live view is served at `/live`. The ESP32-CAM stream is available at `http://<camera-ip>:81/stream`. In the default firmware configuration, the CAM uses `192.168.29.10`; set the address to match your network.

## Requirements

- Python 3.11 is recommended (the Docker image uses Python 3.11).
- PlatformIO Core for firmware builds and USB uploads.
- A supported ESP32-CAM board and, optionally, an ESP32-WROOM board.
- Network access on first server startup so the server can download its YuNet and `buffalo_m` face models.

The object-detection model files are not included in the repository. Place a compatible ONNX model at `models/yolo11s.onnx` or `models/yolov8n.onnx` to enable object detection.

## Run locally

Create and activate a virtual environment, then install the project and server runtime dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install onnxruntime huggingface_hub
```

For Windows DirectML acceleration, install `onnxruntime-directml` instead of `onnxruntime`. The server can use the CPU provider when a GPU provider is not available. `psutil` is optional and enables additional process metrics.

Start the dashboard and Control Center in separate terminals:

```powershell
python server.py
```

```powershell
python control_center.py
```

Open `http://localhost:5000` and `http://localhost:5001`. You can also use `start.bat` (Windows) or `start.sh` (Linux/macOS); these scripts use Docker Compose when Docker is available and otherwise start both Python services.

Set `PORT` to change the dashboard port, `CONTROL_CENTER_PORT` to change the Control Center port, and `MDNS_HOST` to change the dashboard's advertised hostname. The project can also be run with Docker Compose:

```sh
docker compose up --build
```

## Configure and build the firmware

1. Copy `firmware/esp32-cam/include/secrets.example.h` to `firmware/esp32-cam/include/secrets.h`.
2. Set the Wi-Fi credentials and review the example's static IP, gateway, and custom-MAC options. Keep `secrets.h` private; it is excluded from version control.
3. Build from the repository root:

```sh
pio run -d firmware/esp32-cam -e esp32cam
```

The second environment builds the WROOM target:

```sh
pio run -d firmware/esp32-cam -e esp32wroom
```

For USB upload, specify the serial port for the board:

```sh
pio run -d firmware/esp32-cam -e esp32cam -t upload --upload-port COM6
```

Replace `COM6` with the actual port. The default firmware mode is `SYSTEM_MODE_WROOM_CAM`; board profiles and feature switches are in `firmware/esp32-cam/include/configs.h`. The firmware initializes ArduinoOTA, but its current partition table is factory-only; USB upload is the documented update method.

## Useful URLs and API

- Dashboard: `http://localhost:5000`
- Control Center: `http://localhost:5001`
- Dashboard live view: `http://localhost:5000/live`
- Dashboard health: `http://localhost:5000/api/v1/system/healthz`
- ESP32-CAM status: `http://<camera-ip>/status`
- ESP32-CAM still capture: `http://<camera-ip>/capture`
- ESP32-CAM MJPEG stream: `http://<camera-ip>:81/stream`

The server also provides login/security, camera settings, recognition, events, and notification APIs under `/api/` and `/api/v1/`. The device's port-80 API is implemented in `firmware/esp32-cam/src/network/web_api.cpp`.

## Data and repository layout

```text
.
├── server.py                 # Dashboard and recognition service
├── control_center.py         # Administrative Control Center
├── cc_managers.py            # Control Center managers
├── event_db.py               # Event and audit persistence
├── security_hardening.py     # Login and API security controls
├── vlm_engine.py             # Optional ONNX object detector
├── notifications/            # Notification providers and queue
├── templates/                # Dashboard and Control Center pages
├── scripts/                  # Startup and deployment helpers
├── data/                     # Runtime profiles, settings, and databases
├── firmware/esp32-cam/       # PlatformIO firmware project
└── docs/                     # Project notes and roadmap
```

Runtime data and model weights may be excluded from Git. Back up `data/` and any locally supplied model files if you need to preserve them.
