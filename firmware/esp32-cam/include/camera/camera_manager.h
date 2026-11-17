#pragma once
#include <Arduino.h>
#include "app_config.h"

#if HAS_CAMERA_FEATURE
#include <esp_camera.h>

bool initCamera(bool fast_wake = false);
void initCameraStandby();
void updateCamera();

bool isCameraReady();
bool isCameraSensorActive();
bool setCameraSensorActive(bool active);
void requestCameraWakeAsync();
camera_fb_t* captureSnapshotFb();
void returnSnapshotFb(camera_fb_t* fb);

void autoTuneCameraXclk(int wifi_channel);
void recordCaptureMetrics(bool ok, size_t len);
void getCameraDiagnosticsJson(char* buf, size_t max_len);

// Mutex protection for camera sensor register access
bool takeCameraMutex(uint32_t timeout_ms = 1000);
void giveCameraMutex();

#else

inline bool initCamera(bool = false) { return false; }
inline void initCameraStandby() {}
inline void updateCamera() {}
inline bool isCameraReady() { return false; }
inline bool isCameraSensorActive() { return false; }
inline bool setCameraSensorActive(bool) { return false; }
inline void requestCameraWakeAsync() {}
inline void autoTuneCameraXclk(int) {}
inline void getCameraDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"camera\":{\"enabled\":false}");
}
inline bool takeCameraMutex(uint32_t = 0) { return true; }
inline void giveCameraMutex() {}

#endif
