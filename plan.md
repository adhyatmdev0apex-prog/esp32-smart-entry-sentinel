# ESP32-CAM Easy Framework — Roadmap & Architecture Plan

## 1. Goal

Build a clean, beginner-friendly, PlatformIO-first framework for ESP32 projects, centered on the AI-Thinker ESP32-CAM + OV2640.

The same framework should support:

- WROOM-only door monitoring
- CAM-only camera projects
- CAM + reed/PIR
- WROOM + CAM over Wi-Fi
- ESP32-local face detection/finding only
- Laptop/server AI for heavier analysis
- Optional SD storage
- Local web interface
- Optional notifications
- Multiple boards/cameras through hardware profiles

Core principle:

> Configure behaviour; don't rewrite firmware.

---

# 2. Device Roles

## ESP32-WROOM

Use for:

- Reed/door monitoring
- PIR/motion
- Buzzer
- Wi-Fi
- Event generation
- Local logging
- Local web/API
- Sending events to another ESP32

It does not need camera code.

## ESP32-CAM

Use for:

- OV2640 capture
- Snapshot
- MJPEG stream
- SD storage
- Lightweight local face detection
- Sending JPEGs to laptop/server

## Laptop/server

Use for:

- YOLO/person detection
- High-quality face detection
- Face recognition
- Object detection
- Database
- Long-term storage
- Advanced AI

Heavy AI stays off the ESP32.

---

# 3. Wi-Fi Architecture

Every ESP32 has Wi-Fi, so Wi-Fi is the basic communication layer.

```text
WROOM
  │
  │ Wi-Fi event
  ▼
ESP32-CAM
  │
  │ JPEG / stream / event
  ▼
Laptop / AI server
```

Preferred communication:

1. HTTP/REST — core
2. WebSocket — optional live updates
3. UDP — only where useful
4. MQTT — optional future integration

No cloud service should be required.

---

# 4. Modes

## MODE_WROOM_ONLY

Hardware:

- ESP32-WROOM
- Reed and/or PIR
- Optional buzzer

Behaviour:

- Connect Wi-Fi
- Monitor sensors
- Maintain door state
- Log events
- Local web UI
- Optional notifications
- Optional API
- Optional events to another ESP32

Example:

```text
Door opens
  ↓
Reed event
  ↓
WROOM records DOOR_OPEN
  ↓
Web UI updates
  ↓
Optional notification
```

---

## MODE_CAM_ONLY

Hardware:

- ESP32-CAM
- Camera
- Optional SD
- Optional sensor

Behaviour:

- Camera initialization
- Local web UI
- Snapshot
- Stream
- Optional sensor-triggered capture
- Optional SD
- Optional local face detection

---

## MODE_CAM_REED

ESP32-CAM directly monitors the reed.

Door event can trigger:

- Snapshot
- Recording
- Face detection
- Laptop upload

---

## MODE_CAM_MOTION

ESP32-CAM directly monitors PIR/motion.

Motion can trigger the same camera actions.

---

## MODE_WROOM_CAM

Hardware:

- WROOM
- ESP32-CAM
- Reed/PIR on WROOM

Behaviour:

```text
WROOM detects event
      ↓ Wi-Fi
CAM receives event
      ↓
Camera action
      ↓
Optional AI/storage
```

WROOM remains the primary event controller.

---

## MODE_WROOM_CAM_LOCAL_AI

```text
Door opens
   ↓
WROOM
   ↓ Wi-Fi
CAM
   ↓ JPEG
Laptop/server
   ↓
AI result
   ↓
Event/log/notification
```

---

## MODE_WROOM_CAM_LOCAL_FACE

Same architecture, but the CAM performs lightweight face detection.

Detection answers:

```text
Face found: YES
Count: 1
Box: x,y,w,h
Confidence: 0.87
```

It must NOT answer:

```text
Who is this?
```

No recognition or identity database belongs on the ESP32.

---

# 5. Common Event System

All modes use the same event model.

Example:

```cpp
Event {
    type;
    source;
    timestamp;
    device_id;
    value;
}
```

Events:

```text
BOOT
WIFI_CONNECTED
WIFI_DISCONNECTED

DOOR_OPEN
DOOR_CLOSED

MOTION_START
MOTION_END

CAMERA_STARTED
CAMERA_STOPPED
SNAPSHOT_CAPTURED
RECORDING_STARTED
RECORDING_STOPPED

FACE_FOUND
FACE_LOST

AI_REQUEST
AI_RESULT
AI_ERROR

SD_MOUNTED
SD_ERROR

SYSTEM_ERROR
```

This keeps WROOM-only and camera modes compatible.

---

# 6. Door / Reed Behaviour

Reed is the primary door sensor.

Support:

- Normally-open / normally-closed
- Debounce
- Event-on-change
- Event-on-open
- Event-on-close
- Minimum event interval
- Door-open timeout
- Optional buzzer
- Wi-Fi event transmission

Conceptual configuration:

```cpp
door.enabled = true;
door.sensor = REED;
door.active_level = LOW;
door.debounce_ms = 50;
door.open_event = true;
door.close_event = true;
door.open_timeout_ms = 30000;
```

---

# 7. Sensor System

Sensors are modules.

## Reed

Settings:

```text
enabled
GPIO
active level
debounce
event behaviour
```

## PIR

Settings:

```text
enabled
GPIO
active level
warmup
cooldown
event behaviour
```

Future:

- IR
- ultrasonic
- ToF
- vibration
- buttons
- light sensors

The event system should not need rewriting for new sensors.

---

# 8. Camera System

Camera configuration should cover:

```text
camera model
frame size
pixel format
JPEG quality
frame buffers
grab mode
XCLK
horizontal flip
vertical flip
brightness
contrast
saturation
effects
auto exposure
white balance
stream
snapshot
recording
```

Raw GPIO definitions belong only in board profiles.

Example:

```text
boards/
    ai_thinker_esp32_cam.h
```

Application code should simply call:

```cpp
camera.init();
```

---

# 9. ESP32-Local Face Detection

Purpose:

> Detect/find faces, not identify people.

Possible result:

```json
{
  "face_found": true,
  "count": 1,
  "faces": [
    {
      "x": 120,
      "y": 80,
      "w": 96,
      "h": 96,
      "confidence": 0.87
    }
  ]
}
```

Config:

```text
enabled
continuous/event mode
minimum confidence
maximum faces
detection interval
frame size
draw boxes
send events
save detection frame
```

Default behaviour should be event-driven:

```text
Sensor event
  ↓
Capture frame
  ↓
Face detection
  ↓
FACE_FOUND / no face
  ↓
Return to idle
```

This saves CPU, RAM and power.

---

# 10. Laptop / Local AI Server

The laptop/server exposes a simple HTTP API.

Example:

```text
POST /api/v1/analyze
```

Input:

- JPEG
- device ID
- event ID
- timestamp
- metadata

Possible result:

```json
{
  "person_detected": true,
  "person_count": 1,
  "faces_detected": 1,
  "objects": [],
  "identity": null
}
```

Possible server modules:

### Person detection

YOLO or another efficient detector.

### Face detection

Higher-quality laptop-side detector.

### Face recognition

Optional server-side identity matching.

### Object detection

Optional:

```text
person
bag
vehicle
animal
package
...
```

### How VLMs Could Fit Into the Architecture

A Vision-Language Model (e.g., Gemini 1.5/2.0 Flash, Moondream2, Florence-2, LLaVA, or Qwen2-VL) takes images alongside text prompts to generate natural language descriptions (e.g. "A delivery driver wearing a vest is placing a cardboard box on the doorstep").

#### Edge vs Server Workload Separation
- **ESP32 Edge Role**: Frame capture, motion detection, and lightweight face finding. Zero heavy AI computation runs on the microcontroller.
- **Laptop / Server AI Role**: Ingests event snapshots over HTTP (`/api/v1/analyze`), executes facial recognition via SFace, and invokes the VLM asynchronously for scene understanding.

#### Hardware Resource Constraints & Thermal Management
To prevent cooking laptop thermals and cap total system utilization at **≤ 30%** on modern thin-and-light laptops (e.g., AMD Ryzen 5 AI with Radeon 840M iGPU, 16 GB system RAM with 11–12 GB baseline consumption):
1. **Model Size Constraint (< 500–600 MB)**:
   - Target models: **Florence-2-base** (~460 MB) or **Moondream2 INT4/ONNX** (~480 MB).
   - Keeps total VRAM/RAM working set under ~1.2 GB, fitting safely within the available 3–4 GB system memory margin.
2. **Compulsory GPU Offloading (DirectML)**:
   - Inference runs via `onnxruntime-directml` with `DmlExecutionProvider` mapped to the AMD Radeon 840M DirectX 12 compute engine.
   - Offloads matrix multiplication and attention layers from the CPU cores to the GPU, keeping CPU temperatures and fan acoustics quiet.
3. **Event-Driven Asynchronous Throttling**:
   - The VLM is NEVER executed continuously on every video stream frame.
   - It is triggered strictly on high-value system events (e.g., `DOOR_OPEN`, `MOTION_START`, or new person detected).
   - Cooldown pacing (minimum 5 seconds between consecutive inferences) guarantees zero thermal runaway.
4. **Cloud / Zero-Resource Fallback (Gemini 1.5/2.0 Flash)**:
   - Optional API mode utilizing Gemini Flash for 0 MB local model weight, 0% CPU, and 0% GPU utilization.

The ESP32 should not care which model the server uses.

---

# 11. Local AI Config

Conceptually:

```cpp
ai.enabled = true;
ai.mode = AI_LOCAL_SERVER;

ai.host = "192.168.1.50";
ai.port = 8000;
ai.endpoint = "/api/v1/analyze";

ai.timeout_ms = 5000;
ai.retry_count = 2;

ai.send_on = {
    DOOR_OPEN,
    MOTION_START
};

ai.send_snapshot = true;
ai.send_stream = false;
```

Modes:

```text
AI_NONE
AI_ESP32_FACE
AI_LOCAL_SERVER
AI_WEB_SERVER
```

Web/cloud AI is optional.

---

# 12. Storage

Storage modes:

```text
STORAGE_NONE
STORAGE_SD
STORAGE_SERVER
STORAGE_SD_AND_SERVER
```

Settings:

```text
enabled
snapshots
recordings
events
directory structure
maximum file size
retention
overwrite behaviour
```

Suggested layout:

```text
/data/
    YYYY/
        MM/
            DD/
                events/
                snapshots/
                recordings/
```

The ESP32 must work without an SD card.

---

# 13. Web Interface

The UI should expose only features enabled by the current mode.

## WROOM-only

```text
Door state
Last event
Wi-Fi
Uptime
Event history
```

## CAM-only

```text
Camera
Snapshot
Stream
SD
Detection
```

## WROOM + CAM

```text
Door state
Camera state
Last event
Snapshot
AI status
Face detection
Storage
```

Use local access such as:

```text
http://esp-door.local
```

---

# 14. Notifications

Notifications consume the common event system.

Possible providers:

```text
NONE
LOCAL_WEB
HTTP_WEBHOOK
MQTT
TELEGRAM (future)
PUSH SERVICE (future)
```

Example:

```text
DOOR_OPEN
   ↓
Event system
   ↓
Notification provider
```

Do not hard-code a cloud notification provider into the core.

---

# 15. Wi-Fi Configuration

Configurable:

```text
SSID
password
hostname
DHCP/static IP
connection timeout
retry delay
maximum retries
mDNS
AP fallback
AP SSID/password
```

Use:

```text
include/secrets.h
include/secrets.example.h
```

and keep real credentials out of Git.

---

# 16. Device API

Use a small versioned API.

```text
GET  /api/v1/status

POST /api/v1/event
POST /api/v1/camera/start
POST /api/v1/camera/stop
POST /api/v1/camera/snapshot

GET  /api/v1/snapshot
GET  /api/v1/events
```

Example WROOM event:

```json
{
  "event": "DOOR_OPEN",
  "device": "door-wroom",
  "timestamp": 123456
}
```

CAM response:

```json
{
  "ok": true,
  "action": "snapshot_started"
}
```

---

# 17. Everything Config Should Be Able to Control

## Board

```text
board
camera model
PSRAM usage
flash/settings
```

## Mode

```text
device mode
device role
master/slave behaviour
```

## Wi-Fi

```text
SSID
hostname
DHCP/static IP
mDNS
timeouts
reconnect
AP fallback
```

## Sensors

```text
enabled
GPIO
active level
debounce
cooldown
warmup
```

## Door

```text
open/close events
open timeout
buzzer
event behaviour
```

## Camera

```text
resolution
JPEG quality
FPS target
flip
exposure
white balance
stream
snapshot
recording
```

## Storage

```text
SD
snapshots
recordings
retention
paths
```

## ESP32 face detection

```text
enabled
trigger
interval
confidence
max faces
bounding boxes
save frame
```

## Local AI

```text
enabled
host
port
endpoint
timeout
retry
trigger events
send image
send metadata
```

## Web

```text
enabled
port
mDNS
API
authentication (future)
```

## Notifications

```text
provider
events
cooldown
webhook
```

## Logging

```text
serial logging
log level
event logging
timestamps
```

## Reliability

```text
watchdog
Wi-Fi reconnect
camera recovery
sensor recovery
AI retry
maximum retries
```

---

# 18. Configuration Architecture

Avoid a giant pile of unrelated `#define`s.

Use three layers:

```text
User config
    ↓
Board profile
    ↓
Internal feature modules
```

User-facing:

```text
include/configs.h
```

Hardware-specific:

```text
boards/
```

Implementation:

```text
src/
```

Users normally should not touch `src/`.

---

# 19. Repository Structure

```text
ESP32-CAM-Easy-Framework/
│
├── platformio.ini
│
├── include/
│   ├── configs.h
│   ├── secrets.example.h
│   ├── app_config.h
│   └── version.h
│
├── src/
│   ├── main.cpp
│   ├── core/
│   │   ├── events.cpp
│   │   ├── logger.cpp
│   │   └── system.cpp
│   ├── network/
│   │   ├── wifi.cpp
│   │   ├── api.cpp
│   │   └── mdns.cpp
│   ├── sensors/
│   │   ├── reed.cpp
│   │   └── pir.cpp
│   ├── camera/
│   │   ├── camera.cpp
│   │   └── streaming.cpp
│   ├── storage/
│   │   └── storage.cpp
│   ├── detection/
│   │   └── face_detection.cpp
│   ├── ai/
│   │   └── local_ai.cpp
│   └── ui/
│       └── web_ui.cpp
│
├── boards/
│   └── ai_thinker_esp32_cam.h
│
├── examples/
│   ├── wroom_door/
│   ├── cam_only/
│   ├── cam_reed/
│   ├── cam_motion/
│   ├── wroom_cam/
│   ├── wroom_cam_face/
│   └── wroom_cam_local_ai/
│
├── server/
│   ├── README.md
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── detector.py
│       └── config.py
│
├── docs/
│   ├── getting_started.md
│   ├── configuration.md
│   ├── boards.md
│   ├── modes.md
│   ├── sensors.md
│   ├── local_ai.md
│   └── troubleshooting.md
│
├── README.md
└── LICENSE
```

---

# 20. Roadmap

## Phase 0 — Architecture

Define:

- Config model
- Board abstraction
- Event system
- Device roles
- API conventions

No advanced AI yet.

## Phase 1 — WROOM Foundation

Implement:

- PlatformIO
- Wi-Fi
- mDNS
- logging
- web server
- event system
- reed
- door state
- event history

Target:

```text
Stable WROOM-only door monitor
```

## Phase 2 — CAM Foundation

Implement:

- AI-Thinker board profile
- OV2640 initialization
- snapshot
- MJPEG stream
- web interface
- camera config

Target:

```text
Stable CAM-only camera server
```

## Phase 3 — Storage

Implement:

- SD
- snapshots
- recording
- event filenames
- failure handling

## Phase 4 — CAM Sensors

Implement:

- reed
- PIR
- sensor abstraction
- event-triggered capture

## Phase 5 — WROOM ↔ CAM

Implement:

- device discovery/config
- event API
- WROOM → CAM commands
- CAM → WROOM status
- heartbeat
- reconnect/recovery

Target:

```text
WROOM detects door event
        ↓
CAM reacts
```

## Phase 6 — ESP32 Face Detection

Implement:

- frame capture
- lightweight face detector
- confidence
- face count
- bounding boxes
- event generation

Target:

```text
FACE_FOUND
```

Not recognition.

## Phase 7 — Laptop AI

Implement:

- local HTTP server
- image upload
- authentication/token
- JSON results
- person detection
- optional laptop face detection

Target:

```text
ESP32 → Laptop → AI result
```

## Phase 8 — Recognition

Optional server-side layer:

```text
Face detection
    ↓
Embedding
    ↓
Local identity database
    ↓
Recognition
```

## Phase 9 — Event Database

Add:

- SQLite
- event history
- snapshots
- AI results
- timestamps
- device IDs

## Phase 10 — Notifications

Add optional:

- HTTP webhook
- MQTT
- other providers

## Phase 11 — Reliability

Test:

- Wi-Fi loss
- router restart
- camera failure
- SD failure
- AI server offline
- repeated door events
- sensor bounce
- long uptime
- memory leaks
- reboot recovery

Target:

> No manual reset required during normal failures.

## Phase 12 — More Boards/Cameras

Only after AI-Thinker is stable.

Add:

- other OV2640 boards
- 3MP cameras
- 5MP cameras
- other ESP32 camera variants

Each gets a hardware profile.

---

# 21. Showcase Smart-Door Flow

The main example should eventually be:

```text
Door
 ↓
Reed
 ↓
ESP32-WROOM
 ↓
DOOR_OPEN
 ↓ Wi-Fi
ESP32-CAM
 ↓
Snapshot
 ↓
ESP32-local face detection
 ↓
Optional JPEG → Laptop
 ↓
YOLO / face recognition
 ↓
Result
 ↓
Event database
 ↓
Local UI / notification
```

Example event log:

```text
19:42:13  DOOR_OPEN
19:42:14  CAMERA_STARTED
19:42:14  SNAPSHOT_CAPTURED
19:42:15  FACE_FOUND count=1
19:42:16  AI_RESULT person=1
19:42:16  EVENT_STORED
```

---

# 22. Hardware Plan

## Minimum WROOM door setup

- ESP32-WROOM
- Reed switch
- Magnet
- Jumper wires
- Optional buzzer
- USB power

## Minimum CAM setup

- AI-Thinker ESP32-CAM
- OV2640
- USB-to-TTL/programmer
- 5V/appropriate stable power
- Optional microSD

## Combined setup

- WROOM
- ESP32-CAM
- Reed connected to WROOM
- Shared Wi-Fi network
- Optional buzzer
- Optional SD
- Laptop/server for AI

Important:

- Use stable power.
- Keep camera GPIO assignments board-specific.
- Do not assume every ESP32-CAM variant has identical pins.
- Avoid powering the camera from an inadequate USB-TTL adapter.
- Keep sensor wiring simple and documented.

---

# 23. Security

Even on LAN:

- Never commit Wi-Fi passwords.
- Do not expose the AI server to the internet by default.
- Support API tokens.
- Validate incoming events.
- Limit image upload size.
- Reject unsafe file paths.
- Keep cloud integrations opt-in.
- Default to local-only operation.

---

# 24. Definition of "Easy"

A beginner should be able to:

1. Install PlatformIO.
2. Open the repository.
3. Copy `secrets.example.h` → `secrets.h`.
4. Enter Wi-Fi credentials.
5. Select a mode in `configs.h`.
6. Select the board.
7. Build.
8. Flash.
9. Open `http://device.local`.
10. Use the enabled features.

Changing:

```text
WROOM-only
```

to:

```text
WROOM + CAM
```

should mainly require configuration + wiring, not firmware rewriting.

---

# 25. First Stable Release

The first useful release should contain only:

```text
AI-Thinker ESP32-CAM
OV2640
WROOM
Wi-Fi
mDNS
local web UI
reed
PIR
snapshot
stream
SD snapshots
event system
WROOM ↔ CAM API
basic ESP32 face detection
configuration system
PlatformIO
```

Then add laptop AI.

Do NOT make recognition, cloud deployment, MQTT, databases, or many camera boards prerequisites for the first stable release.

---

# 26. Success Criteria

The project succeeds when:

- Beginners understand the architecture quickly.
- One config file controls normal behaviour.
- AI-Thinker ESP32-CAM works reliably.
- WROOM-only works without camera code.
- CAM-only works without WROOM code.
- WROOM + CAM communicates over Wi-Fi.
- Face detection works without recognition.
- Laptop AI can be added without rewriting camera firmware.
- Storage can be enabled/disabled from config.
- Sensors can be enabled/disabled from config.
- Board-specific hardware is isolated.
- Wi-Fi failures recover automatically.
- Examples compile independently.
- Documentation matches firmware.
- The core stays maintainable.

---

# 27. Guiding Rule

Build the boring foundation first.

```text
CONFIG
  ↓
BOARD
  ↓
DEVICE ROLE
  ↓
SENSORS / CAMERA
  ↓
EVENT SYSTEM
  ↓
Wi-Fi API
  ↓
STORAGE
  ↓
LOCAL FACE DETECTION
  ↓
LAPTOP AI
  ↓
RECOGNITION / ADVANCED FEATURES
```

If a feature makes beginner setup significantly harder, it belongs in an optional module — not in the core.
