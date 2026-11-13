#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

extern "C" esp_err_t esp_base_mac_addr_set(const uint8_t *mac);

#include "camera.h"
#include "network.h"
#include "stream_server.h"

void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); // Disable brownout detector
  
  // Set chip base MAC to 12:11:11:11:11:11 before any WiFi initialization
  uint8_t custom_mac[6] = {0x12, 0x11, 0x11, 0x11, 0x11, 0x11};
  esp_base_mac_addr_set(custom_mac);

  Serial.begin(115200);
  delay(1000);
  Serial.println("\n==================================");
  Serial.println("  ESP32-CAM Smart Streamer Booting");
  Serial.println("==================================");

  // 1. Initialize camera
  bool cameraReady = false;
  for (int attempt = 1; attempt <= 3; attempt++) {
    Serial.printf("Initializing camera (attempt %d/3)...\n", attempt);
    if (initializeCamera()) {
      cameraReady = true;
      Serial.println("Camera initialized successfully!");
      break;
    }
    delay(1000);
  }

  // 2. Connect to WiFi
  connectToWiFi();

  if (cameraReady && WiFi.status() == WL_CONNECTED) {
    startCameraServer();
    ArduinoOTA.setHostname("esp32cam-door");
    ArduinoOTA.begin();
    Serial.printf("\n>>> STREAM IS LIVE AT: http://%s:81/stream <<<\n", WiFi.localIP().toString().c_str());
    Serial.println(">>> OTA Wireless Flashing Ready! <<<\n");
  } else if (!cameraReady) {
    Serial.println("ERROR: Camera failed to initialize! Check ribbon cable.");
  }
}

void loop() {
  ArduinoOTA.handle();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected! Reconnecting...");
    WiFi.reconnect();
    delay(4000);
  }
  delay(1);
}
