#pragma once

/**
 * ============================================================================
 * ESP32-CAM EASY FRAMEWORK — USER CONFIGURATION
 * Edit this single file to choose device mode, sensors, and camera features.
 * ============================================================================
 */

// ============================================================================
// 1. OPERATION MODE (Uncomment EXACTLY ONE mode below)
// ============================================================================
#if !defined(SYSTEM_MODE_WROOM_ONLY) && !defined(SYSTEM_MODE_CAM_ONLY) && !defined(SYSTEM_MODE_WROOM_CAM)
  // #define SYSTEM_MODE_WROOM_ONLY   // Standalone WROOM: monitors door/motion, no camera code
  // #define SYSTEM_MODE_CAM_ONLY     // Standalone CAM: camera, live stream, cooling, web API
  #define SYSTEM_MODE_WROOM_CAM       // Combined: WROOM sends events over Wi-Fi -> CAM captures
#endif

// ============================================================================
// 2. DEVICE IDENTITY & NETWORK
// ============================================================================
#define CFG_DEVICE_ID               "esp-door-cam-01"
#define CFG_MDNS_HOSTNAME           "esp-door"
#define CFG_HTTP_PORT               80
#define CFG_STREAM_PORT             81
#define CFG_WIFI_RECONNECT_INTERVAL 10000 // ms between reconnect attempts

// ============================================================================
// 3. SENSORS (Configure based on hardware you actually own)
// ============================================================================
// --- Magnetic Reed Door Switch ---
#define CFG_ENABLE_REED             0       // 1 = enabled, 0 = disabled
#define CFG_REED_ACTIVE_LEVEL       LOW     // LOW when magnet is near (closed door)
#define CFG_REED_DEBOUNCE_MS        50      // Contact bounce filter
#define CFG_REED_OPEN_TIMEOUT_MS    30000   // Alert if door stays open longer than this

// --- PIR Motion Sensor ---
#define CFG_ENABLE_PIR              0       // 1 = enabled, 0 = disabled
#define CFG_PIR_ACTIVE_LEVEL        HIGH    // HIGH when motion detected
#define CFG_PIR_WARMUP_MS           15000   // Sensor warm-up time after boot
#define CFG_PIR_COOLDOWN_MS         3000    // Minimum cooldown between motion triggers

// --- Door Alert Buzzer ---
#define CFG_ENABLE_BUZZER           0       // 1 = enabled, 0 = disabled
#define CFG_BUZZER_ACTIVE_LEVEL     HIGH

// ============================================================================
// 4. CAMERA & STREAMING (Ignored in WROOM-only mode)
// ============================================================================
#define CFG_ENABLE_CAMERA           1       // 1 = initialize camera, 0 = disable
#define CFG_CAMERA_FRAME_SIZE       9       // 9 = FRAMESIZE_SVGA (800x600) default
#define CFG_CAMERA_JPEG_QUALITY     12      // 10-63 (lower number = higher quality)
#define CFG_CAMERA_XCLK_MHZ         20      // 20 MHz standard OV2640 clock
#define CFG_CAMERA_HFLIP            0
#define CFG_CAMERA_VFLIP            0
#define CFG_ENABLE_STREAMING        1       // 1 = live MJPEG stream on port 81

// ============================================================================
// 5. ADAPTIVE PWM COOLING CONTROLLER (GPIO 13, IRFZ44N Low-Side MOSFET)
// ============================================================================
#define CFG_ENABLE_COOLING          1       // 1 = enable adaptive fan control
#define CFG_COOLING_MIN_DUTY_PCT    70      // 70% absolute minimum fan speed floor
#define CFG_COOLING_TEMP_STEP_DEG_C 5.0f    // 5°C temperature rise per step
#define CFG_COOLING_STEP_DUTY_PCT   5       // 5% duty increase per step
#define CFG_COOLING_PWM_FREQ_HZ     25000   // 25 kHz ultrasonic (silent, no whine)
#define CFG_COOLING_REF_TEMP_C      60.0f   // Reference temperature baseline
#define CFG_COOLING_XGA_THRESH_C    72.0f   // XGA 100% full-speed threshold

// ============================================================================
// 6. MICROSD STORAGE (Optional, camera operates normally if disabled)
// ============================================================================
#define CFG_ENABLE_SD_STORAGE       0       // 1 = mount MicroSD if inserted, 0 = disabled

// ============================================================================
// 7. ESP32-LOCAL FACE DETECTION (Pure Finding/Detection ONLY — NO Recognition)
// ============================================================================
#define CFG_ENABLE_FACE_DETECTION   0       // 1 = lightweight local face finder
#define CFG_FACE_MAX_DETECT         4       // Maximum bounding boxes returned
#define CFG_FACE_MIN_CONFIDENCE     0.70f

// ============================================================================
// 8. LAPTOP / LOCAL AI SERVER (Optional integration)
// ============================================================================
#define CFG_ENABLE_LOCAL_AI         0       // 1 = POST snapshots to AI server
#define CFG_LOCAL_AI_HOST           "192.168.29.6"
#define CFG_LOCAL_AI_PORT           5000
#define CFG_LOCAL_AI_ENDPOINT       "/api/v1/analyze"
#define CFG_LOCAL_AI_TIMEOUT_MS     4000
#define CFG_LOCAL_AI_RETRIES        2

// ============================================================================
// 9. WROOM <-> CAM LINK (Used when SYSTEM_MODE_WROOM_CAM is active)
// ============================================================================
#define CFG_PEER_CAM_IP             "192.168.29.10"
#define CFG_PEER_WROOM_IP           "192.168.29.20"
#define CFG_LINK_TIMEOUT_MS         500
