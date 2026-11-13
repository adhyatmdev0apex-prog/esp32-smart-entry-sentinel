#include "detection/face_detection.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"
#include <stdio.h>

#if HAS_FACE_DETECTION
static uint32_t s_detect_count = 0;

void initFaceDetection() {
  logInfo("FACE", "ESP32-local Face Finder initialized (Phase 6 detection abstraction)");
}

FaceDetectionResult detectFaces(const uint8_t* jpeg_data, size_t length) {
  FaceDetectionResult result = {};
  if (!jpeg_data || length == 0) return result;

  // Lightweight detection pipeline (interface stub ready for esp-face / MSR01)
  // Currently verifies frame presence without blocking RTOS streaming
  s_detect_count++;
  result.face_found = false;
  result.count = 0;

  return result;
}

void getFaceDetectionDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"face_detection\":{\"enabled\":true,\"invocations\":%u}",
           (unsigned int)s_detect_count);
}

#else

void initFaceDetection() {}
FaceDetectionResult detectFaces(const uint8_t*, size_t) {
  return FaceDetectionResult{false, 0, {}};
}
void getFaceDetectionDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"face_detection\":{\"enabled\":false}");
}

#endif
