# 🏠 ESP32-CAM Smart Door Entry & Dual-AI Monitoring Framework

A modular, distributed ESP32-CAM + ESP32-WROOM security firmware and DirectML GPU-accelerated Python AI server (`server.py`) with real-time **512-D Face Recognition** and **YOLOv8n Object Identification**.

![PlatformIO](https://img.shields.io/badge/PlatformIO-Compatible-orange?logo=platformio)
![ESP32-CAM](https://img.shields.io/badge/AI--Thinker-ESP32--CAM-blue)
![ESP32-WROOM](https://img.shields.io/badge/ESP32-WROOM-red)
![Camera](https://img.shields.io/badge/Sensor-OV2640-green)
![AI](https://img.shields.io/badge/DirectML%20GPU-ArcFace%20%2B%20YOLOv8n-indigo)
![Python](https://img.shields.io/badge/Server-Python%203%20%7C%20ONNX%20DirectML-yellow?logo=python)
![License](https://img.shields.io/badge/License-MIT-purple)

---

## ✨ Features

### 1. Distributed Edge Hardware & Modes (`firmware/esp32-cam`)
- **Single-Place Mode Switching (`include/configs.h`)**: Switch between `SYSTEM_MODE_WROOM_ONLY`, `SYSTEM_MODE_CAM_ONLY`, and `SYSTEM_MODE_WROOM_CAM` from one header; `/status` reports the active mode (`WROOM_ONLY`, `CAM_ONLY`, or `WROOM_CAM`).
- **Dual-Board Static IP & Custom MAC Support (`include/secrets.h`)**:
  - **ESP32-CAM**: `192.168.29.10` (`12:11:11:11:11:11`)
  - **ESP32-WROOM**: `192.168.29.20` (`12:11:11:11:11:20`)
- **Clean OV2640 Camera Recovery & Low-Latency Streaming**:
  - Automatic `PIN_CAM_PWDN` hardware power-cycle (`250 ms HIGH -> 100 ms LOW`) on boot to clear latched sensor states across warm/OTA reboots.
  - `esp_camera_deinit()` on OTA start so firmware updates cleanly stop DMA/I2S before flashing.
  - Dedicated Core 1 MJPEG server on **Port 81** (`/stream`) with `stage` diagnostics exposed in `/status`.
- **Full Live OV2640 Camera Controls (`/control` & `/api/camera/settings`)**:
  - Dynamically adjust `framesize`, `quality` (10–63), `brightness`, `contrast`, `saturation`, `special_effect`, `vflip`, `hmirror`, `awb`, `aec`, and onboard Flash LED PWM (`flash` on GPIO 4) with read-back verification.
- **25 kHz Ultrasonic Adaptive PWM Cooling (`cooling.cpp`)**:
  - Drives an IRFZ44N MOSFET on **GPIO 13** above the audible range (`25 kHz`), with a **70% minimum floor**, `+5%` per `5°C` stepping above `60°C`, kickstart boost, and resolution-aware overrides.
- **Wireless OTA & Serial Flashing**:
  - ArduinoOTA on port `3232` via `ota_upload.py`, plus standard PlatformIO USB/COM flashing.

### 2. Dual-AI Vision Engine (`server.py` + `vlm_engine.py`)
- **Engine 1 — Face Recognition (`YuNet` + `InsightFace MobileFaceNet buffalo_s 512-D ArcFace`)**:
  - **DirectML GPU Acceleration**: Runs `model.onnx` on `DmlExecutionProvider` (AMD Radeon 840M iGPU) with fallback to `CPUExecutionProvider`.
  - **Strict Quality & Geometry Gating**: Rejects tiny faces (`< 32x36 px`, `max < 40 px`), motion blur (`lap_var < 7.0` and `sobel_energy < 110.0`), extreme yaw, and low overall quality (`MIN_QUALITY_FOR_MATCH = 26.0`, `DETECTION_CONF_THRESHOLD = 0.58`).
  - **Anti-Poisoning Profile Protection**: Calibrated cosine similarity thresholds (`SFACE_MATCH_STRONG = 0.48`, `SFACE_MATCH_STANDARD = 0.42`, `SFACE_INCONCLUSIVE_LOW = 0.32`) with margin and centroid checks. Borderline or unclear detections are **never** saved into an enrolled person's folder or added to their gallery (`quality >= 40.0`, `det_score >= 0.72`, `sim_score >= 0.50`, `centroid_sim >= 0.35` required).
  - **Multi-Frame Unknown Confirmation & Duplicate Review**: Requires `3` consistent high-quality sightings before creating a new `person_N`, plus side-by-side duplicate detection and profile merging.
- **Engine 2 — Real-Time Object Identification (`YOLOv8n-320 ONNX`, DirectML GPU)**:
  - Compact **`12.1 MB`** `models/yolov8n.onnx` model detecting **80 COCO object classes** (`person`, `backpack`, `handbag`, `suitcase`, `cell phone`, `chair`, `bottle`, `laptop`, etc.) on `DmlExecutionProvider`.
  - Draws **cyan-blue bounding boxes with object names and confidence percentages** directly onto the live MJPEG stream alongside green/yellow face boxes (even when no faces are visible).
- **Thermal & CPU Ceiling (`<= 40%` Total CPU)**:
  - Both ONNX Runtime sessions use `DmlExecutionProvider`, `intra_op_num_threads = 2`, `inter_op_num_threads = 1`, and `session.intra_op.allow_spinning = 0`.
  - `ai_worker_thread` paces at `0.18 s`, and `ObjectDetectionEngine` enforces a `0.45 s` cooldown plus automatic backoff if `psutil.cpu_percent()` exceeds `36%`.

### 3. Web Dashboard (`templates/index.html`, Port 5000)
- **Independent AI Toggles**: Separate buttons to turn **Face Recognition** (`🧠 Recognition: ON/OFF`) and **Object Identification** (`📦 Objects: ON/OFF`) on or off independently, plus **`🔲 Boxes: ON/OFF`** overlay control.
- **Live Object Banner**: Shows real-time detected object names and confidences in the **Detected Objects & Bounding Boxes (DirectML GPU)** panel.
- **System & Device Reboot Controls (inside Settings)**:
  - **`📷 Reboot CAM`** (`192.168.29.10`)
  - **`📡 Reboot WROOM`** (`192.168.29.20`)
  - **`⚡ Full Reboot (All + Models + Refresh)`**: Reboots both ESP32 boards, reloads both the Face Recognition and DirectML Object Detection models on the server, clears stale frame queues, and refreshes the dashboard.
- **Identity Management**: Rename persons, inspect/delete individual face crops & quality scores, merge duplicates, view live recognition events, and reset data.

---

## 🎯 Project Architecture

```text
ESP32-WROOM (192.168.29.20 — Door Reed, PIR, Buzzer)
       │
       ▼ HTTP POST /api/v1/event (Wi-Fi)
ESP32-CAM   (192.168.29.10 — OV2640, Port 81 MJPEG, Port 80 REST, 25kHz Fan)
       │
       ▼ MJPEG Stream (:81/stream)
Laptop / AI Server (server.py + vlm_engine.py, Port 5000)
  ├── Face Engine:   YuNet + MobileFaceNet 512-D ArcFace (DirectML GPU)
  ├── Object Engine: YOLOv8n-320 ONNX, 80 COCO classes   (DirectML GPU)
  └── Web Dashboard: Live Boxes, Controls, Gallery & Device Reboots
```

---

## 🏗️ Hardware Profiles

Pin definitions are isolated in `firmware/esp32-cam/boards/` and routed by `boards/boards.h`:

1. **AI-Thinker ESP32-CAM** (`boards/ai_thinker_esp32_cam.h`)
   - **SoC**: ESP32-D0WD with 4 MB external PSRAM
   - **Camera**: OV2640 (D0–D7, PCLK, XCLK, VSYNC, HREF, SIOC, SIOD, `PWDN` on **GPIO 32**)
   - **Cooling Fan**: IRFZ44N MOSFET gate on **GPIO 13** (25 kHz LEDC PWM, 70% min floor)
   - **Flash LED**: **GPIO 4** (LEDC PWM)
   - **MicroSD**: 1-bit SD_MMC mode (pins 2, 14, 15)

2. **ESP32-WROOM DevKit** (`boards/esp32_wroom_devkit.h`)
   - **Reed Switch**: **GPIO 4** | **PIR Motion**: **GPIO 14** | **Buzzer**: **GPIO 15** | **Status LED**: **GPIO 2**

---

## ⚙️ Operating Modes (`firmware/esp32-cam/include/configs.h`)

Select exactly one mode in `configs.h`:
- `SYSTEM_MODE_WROOM_ONLY`: Standalone WROOM sensor node (no camera/PSRAM required).
- `SYSTEM_MODE_CAM_ONLY`: Standalone ESP32-CAM (camera, Port 81 stream, 25 kHz cooling, REST API).
- `SYSTEM_MODE_WROOM_CAM`: Distributed dual-board setup — WROOM forwards door/motion events to CAM (`192.168.29.10`), while both boards stay fault-tolerant if either reboots.

---

## 🧠 Vision Models & Compatible Alternatives

| Task | Active Model | Size / Runtime | Alternatives (DirectML ONNX) |
| :--- | :--- | :--- | :--- |
| **Face Detection** | `YuNet` (`face_detection_yunet_2023mar.onnx`) | ~230 KB / OpenCV DNN | RetinaFace / SCRFD |
| **Face Recognition** | `InsightFace MobileFaceNet` (`buffalo_s/recognition/model.onnx`, 512-D ArcFace) | ~13 MB / `DmlExecutionProvider` | `buffalo_l` (`w600k_r50.onnx`), `buffalo_sc`, `SFace` (`face_recognition_sface_2021dec.onnx`) |
| **Object Identification** | `YOLOv8n-320` (`models/yolov8n.onnx`, 80 COCO classes) | 12.1 MB / `DmlExecutionProvider` | `YOLO11n`, `YOLOv8s`, `YOLOv8n-worldv2` (open-vocabulary) |

---

## 🚀 Quick Start Guide

### 1. Configure & Flash Firmware (PlatformIO)
1. Copy `firmware/esp32-cam/include/secrets.example.h` to `firmware/esp32-cam/include/secrets.h` and set your Wi-Fi credentials / static IPs / MACs.
2. Select your mode in `firmware/esp32-cam/include/configs.h` (e.g. `#define SYSTEM_MODE_WROOM_CAM`).
3. Build and flash:
   ```bash
   # Build
   pio run -d firmware/esp32-cam -e esp32cam
   pio run -d firmware/esp32-cam -e esp32wroom

   # Upload over USB/Serial
   pio run -d firmware/esp32-cam -e esp32cam -t upload --upload-port COM6

   # Upload wirelessly over OTA
   python ota_upload.py -i 192.168.29.10 -f firmware/esp32-cam/.pio/build/esp32cam/firmware.bin
   python ota_upload.py -i 192.168.29.20 -f firmware/esp32-cam/.pio/build/esp32wroom/firmware.bin
   ```

### 2. Run the Python AI Server
```bash
pip install -r requirements.txt
python server.py
```
Open **`http://localhost:5000`** in your browser.

---

## 📡 API Reference

### ESP32 Web API (Port 80)
| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/ping` | `GET` | Health check |
| `/status` / `/api/v1/status` | `GET` | Diagnostics (`mode`, heap, PSRAM, temp, Wi-Fi, camera, stream `stage`, cooling) |
| `/events` / `/api/v1/events` | `GET` | Ring buffer of recent system & sensor events |
| `/api/v1/event` | `POST` | Inter-device event ingest (WROOM -> CAM) |
| `/capture` / `/api/v1/snapshot` | `GET` | Instant JPEG snapshot |
| `/control?var=...&val=...` | `GET` | Live tuning (`framesize`, `quality`, `brightness`, `contrast`, `saturation`, `special_effect`, `vflip`, `hmirror`, `awb`, `aec`, `flash`, `fan_ref_temp`, `fan_xga_thresh`, `fan_freq`, `txpower`, `wifi_proto`) |
| `/reboot` | `GET` | Gracefully restart the microcontroller |

### ESP32-CAM Stream Server (Port 81)
- `GET http://<device-ip>:81/stream` — Low-latency MJPEG stream (Core 1).

### Python AI Server (`server.py`, Port 5000)
| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/` | `GET` | Web dashboard |
| `/live` | `GET` | Annotated MJPEG stream (face boxes + object boxes) |
| `/stream/status` | `GET` | Live FPS, camera health, recognition stats, and detected objects JSON |
| `/api/recognition/toggle` | `POST` | Toggle Face Recognition ON/OFF |
| `/api/v1/vlm/toggle` | `POST` | Toggle Object Detection (`YOLOv8n`) ON/OFF |
| `/api/v1/vlm/describe` | `POST` | Run on-demand object scan on latest/uploaded frame |
| `/api/toggle_overlay` | `POST` | Toggle live bounding box overlays ON/OFF |
| `/api/camera/settings` | `GET`/`POST` | Read or apply ESP32-CAM sensor & flash controls with read-back check |
| `/api/system/reboot_cam` | `POST` | Reboot ESP32-CAM (`192.168.29.10`) |
| `/api/system/reboot_wroom` | `POST` | Reboot ESP32-WROOM (`192.168.29.20`) |
| `/api/system/full_reboot` | `POST` | Reboot CAM + WROOM and reload Face + Object AI models |
| `/api/persons` | `GET` | List enrolled identity profiles and quality metrics |
| `/api/merge` | `POST` | Merge duplicate person profiles |
| `/api/v1/analyze` | `POST` | Analyze a single uploaded JPEG frame |
| `/reset` | `POST` | Clear stored faces, detections, and profiles |

---

## 📁 Repository Structure

```text
.
├── archives/                            # Backups, zips & legacy reference snapshots
├── data/                                # Runtime DBs (identity_profiles.pkl, detections.json, person_labels.json)
├── docs/                                # Architecture & planning docs (plan.md, ROADMAP.md)
├── entrance_monitoring/                 # Captured face gallery & full images
├── firmware/esp32-cam/
│   ├── platformio.ini                   # Environments: esp32cam, esp32wroom
│   ├── boards/                          # ai_thinker_esp32_cam.h, esp32_wroom_devkit.h, boards.h
│   ├── include/                         # configs.h, app_config.h, secrets.h, core/camera/network/sensors
│   └── src/                             # main.cpp, camera_manager.cpp, streaming.cpp, cooling.cpp, web_api.cpp, ...
├── models/
│   ├── face_detection_yunet_2023mar.onnx
│   ├── insightface/recognition/model.onnx
│   ├── face_recognition_sface_2021dec.onnx
│   └── yolov8n.onnx                     # 12.1 MB DirectML object detector (320x320, 80 COCO classes)
├── scripts/
│   ├── ota_upload.py                    # Wireless OTA upload utility
│   ├── download_models.sh               # Runtime ONNX model downloader
│   ├── run_full_validation.py           # End-to-end validation suite
│   ├── test_capture.py                  # Snapshot capture test helper
│   └── test_artifacts/                  # Saved test JPEGs
├── templates/index.html                 # Responsive web dashboard
├── server.py                            # Flask AI server (DirectML Face + Object Detection)
├── vlm_engine.py                        # DirectML YOLOv8n ObjectDetectionEngine
├── requirements.txt
└── README.md
```

---

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
