#pragma once
#include <Arduino.h>
#include "app_config.h"

#if HAS_CAMERA_FEATURE
#include <esp_camera.h>

bool initCamera();
void updateCamera();

bool isCameraReady();
camera_fb_t* captureSnapshotFb();
void returnSnapshotFb(camera_fb_t* fb);

void autoTuneCameraXclk(int wifi_channel);
void recordCaptureMetrics(bool ok, size_t len);
void getCameraDiagnosticsJson(char* buf, size_t max_len);

// Mutex protection for camera sensor register access
bool takeCameraMutex(uint32_t timeout_ms = 1000);
void giveCameraMutex();

#else

inline bool initCamera() { return false; }
inline void updateCamera() {}
inline bool isCameraReady() { return false; }
inline void autoTuneCameraXclk(int) {}
inline void getCameraDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"camera\":{\"enabled\":false}");
}
inline bool takeCameraMutex(uint32_t = 0) { return true; }
inline void giveCameraMutex() {}

#endif
