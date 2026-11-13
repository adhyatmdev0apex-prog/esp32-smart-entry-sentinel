# ESP32-CAM Easy Framework — Project Roadmap

This roadmap defines the strict progression of features and releases.
Follow roadmap discipline: do not implement later phases prematurely. Build the boring, solid foundation first.

---

## Phase 0 — Architecture & Refactoring (Current Phase)
Define:
- Config model (`include/configs.h`, `include/app_config.h`, `include/secrets.example.h`)
- Board abstraction (`boards/ai_thinker_esp32_cam.h`, `boards/esp32_wroom_devkit.h`)
- Unified event system (`Event`, event types, subscribers, queue/dispatcher)
- Device roles and modes (`MODE_WROOM_ONLY`, `MODE_CAM_ONLY`, `MODE_WROOM_CAM`)
- Core API conventions (`/status`, `/events`, `/snapshot`, `/ping`, `/api/v1/...`)
- PlatformIO multi-environment support (`esp32cam` and `esp32wroom`)

---

## Phase 1 — WROOM Foundation
Implement:
- PlatformIO build for WROOM
- Wi-Fi connection manager with robust auto-reconnect
- mDNS discovery (`esp-door.local`)
- Structured logging
- Lightweight web server
- Event system runtime
- Reed switch sensor driver (active level, debounce, door open/close state tracking)
- Optional buzzer support
- Event history logging

Target: Stable WROOM-only door monitor.

---

## Phase 2 — CAM Foundation
Implement:
- AI-Thinker board profile with isolated camera pins and PSRAM handling
- OV2640 sensor initialization with auto-recovery and PWDN cycle
- Snapshot capture
- Low-latency MJPEG stream on dedicated port
- Lightweight control API
- Adaptive cooling integration (PWM fan control)
- Camera configuration (resolution, quality, flip, XCLK tuning)

Target: Stable CAM-only camera server.

---

## Phase 3 — Storage (Optional Module)
Implement:
- Optional microSD mount and detection
- Snapshot file storage with predictable path structure (`/data/YYYY/MM/DD/...`)
- Non-blocking writes, graceful fallback when SD is missing or full
- No camera degradation when SD is disabled

---

## Phase 4 — CAM Sensors (Direct Sensor Mode)
Implement:
- Direct reed switch and PIR motion sensors on CAM where pins permit
- Sensor abstraction layer
- Event-triggered camera capture

---

## Phase 5 — WROOM ↔ CAM Communication
Implement:
- Device discovery / IP configuration over Wi-Fi
- Event transmission (WROOM door/motion event -> CAM capture trigger)
- CAM status and acknowledgement back to WROOM
- Heartbeat and offline tolerance (neither device crashes if the other is unavailable)

---

## Phase 6 — ESP32 Face Detection (Lightweight Detection Only)
Implement:
- ESP32-local face finding/detection (box, count, confidence)
- Pure detection ONLY — NO face recognition, NO identity database on ESP32
- Event generation (`FACE_FOUND`)

---

## Phase 7 — Laptop / Local AI Server
Implement:
- Python Flask / FastAPI server endpoint (`/api/v1/analyze`)
- Standardized JSON response (person detected, face count, etc.)
- Model-agnostic firmware interface

---

## Phase 8 — Face Recognition & Identity Matching (Server-Side)
Implement on the server:
- Embedding extraction
- Identity gallery / profiles
- Unknown face clustering and merging

---

## Phase 9 — Event Database & Analytics
Implement:
- SQLite or persistent event history
- Timeline, charts, audit log

---

## Phase 10 — Notifications
Implement:
- Webhook dispatch
- Optional MQTT / Telegram / push notifications

---

## Phase 11 — Long-Term Reliability & Hardening
Implement:
- Watchdog timer
- Router reboot survival
- Memory leak checks and long-term stability testing

---

## Phase 12 — Additional Boards & Camera Sensors
- Additional OV2640 modules, 3MP/5MP sensors, custom hardware profiles.
