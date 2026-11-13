#include "network/ota_manager.h"
#include "app_config.h"
#include "core/logger.h"
#include <ArduinoOTA.h>
#if HAS_CAMERA_FEATURE
#include <esp_camera.h>
#endif

void initOTA() {
  ArduinoOTA.setPort(3232);
  ArduinoOTA.setHostname(CFG_MDNS_HOSTNAME);

  ArduinoOTA.onStart([]() {
#if HAS_CAMERA_FEATURE
    esp_camera_deinit();
#endif
    String type = (ArduinoOTA.getCommand() == U_FLASH) ? "sketch" : "filesystem";
    logInfo("OTA", "Updating %s...", type.c_str());
  });

  ArduinoOTA.onEnd([]() {
    logInfo("OTA", "Update complete. Rebooting...");
  });

  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    // Avoid spamming log, but keep alive
  });

  ArduinoOTA.onError([](ota_error_t error) {
    logError("OTA", "Update error [%u]", (unsigned int)error);
  });

  ArduinoOTA.begin();
  logInfo("OTA", "ArduinoOTA initialized on port 3232 (hostname: %s)", CFG_MDNS_HOSTNAME);
}

void updateOTA() {
  ArduinoOTA.handle();
}
