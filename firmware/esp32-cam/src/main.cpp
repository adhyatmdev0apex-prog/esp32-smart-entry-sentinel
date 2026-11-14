#include <Arduino.h>
#include "app_config.h"
#include "core/logger.h"
#include "core/system.h"
#include "core/events.h"
#include "sensors/reed.h"
#include "sensors/pir.h"
#include "sensors/buzzer.h"
#include "camera/camera_manager.h"
#include "camera/streaming.h"
#include "camera/cooling.h"
#include "storage/storage_manager.h"
#include "detection/face_detection.h"
#include "ai/local_ai.h"
#include "communication/wroom_cam_link.h"
#include "network/wifi_manager.h"
#include "network/mdns_manager.h"
#include "network/ota_manager.h"
#include "network/web_api.h"

// System event handler: react to sensor or system events
static void onSystemEvent(const Event& event) {
  if (event.type == EVENT_DOOR_OPEN) {
    playTwoToneAlert(3000); // 3-second calm med/low passive buzzer chime
  }
}

#if !BOARD_HAS_CAMERA
static void sensorTaskCore0(void* param) {
  for (;;) {
    updateReedSensor();
    updatePirSensor();
    updateBuzzer();
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}
#endif

void setup() {
  initSystem();
  initLogger(115200);

  logInfo("MAIN", "==================================================");
  logInfo("MAIN", "  %s (v%s)", FRAMEWORK_NAME, FRAMEWORK_VERSION);
  logInfo("MAIN", "  Board: %s | Device: %s", BOARD_NAME, CFG_DEVICE_ID);
  logInfo("MAIN", "==================================================");

  // 1. Common Event System
  initEventSystem();
  registerEventSubscriber(onSystemEvent);

  // 2. Hardware Actuator Safety Initialization
#if (PIN_FLASH_LED >= 0)
  pinMode(PIN_FLASH_LED, OUTPUT);
  digitalWrite(PIN_FLASH_LED, LOW);
#endif

  // 3. Sensors (Door Reed, PIR Motion, Buzzer)
  initReedSensor();
  initPirSensor();
  initBuzzer();

  // 4. Camera & Cooling (CAM modes only)
#if HAS_CAMERA_FEATURE
  initCamera();
  initCooling();
#endif

  // 5. Storage (MicroSD optional)
  initStorage();

  // 6. Vision & AI Client Abstractions
  initFaceDetection();
  initLocalAiClient();

  // 7. Network & Web Services
  initWiFi();
  initMDNS();
  initOTA();
  initWebApi();
  initWroomCamLink();

  // 8. Video Streaming Server (CAM modes only)
#if HAS_CAMERA_FEATURE
  initStreamingServer();
#endif

  postEvent(EVENT_BOOT, "MAIN");

#if !BOARD_HAS_CAMERA
  xTaskCreatePinnedToCore(sensorTaskCore0, "sensor_c0", 4096, nullptr, 2, nullptr, 0);
  logInfo("MAIN", "Sensor & buzzer task pinned to Core 0 (5ms tick).");
#endif

  logInfo("MAIN", "Startup complete. System operational.");
}

void loop() {
  updateSystem();
#if BOARD_HAS_CAMERA
  updateReedSensor();
  updatePirSensor();
  updateBuzzer();
#endif

#if HAS_CAMERA_FEATURE
  updateCamera();
  updateCooling();
#endif

  updateWiFi();
  updateOTA();
  updateWebApi();
  updateWroomCamLink();

  delay(2);
}
