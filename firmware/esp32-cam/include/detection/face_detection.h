#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * ESP32-LOCAL FACE FINDER (DETECTION ONLY)
 * Pure lightweight presence/bounding box detection.
 * NO identity databases, NO face recognition models belong on the ESP32.
 * ============================================================================
 */

struct FaceBox {
  int x;
  int y;
  int w;
  int h;
  float confidence;
};

struct FaceDetectionResult {
  bool face_found;
  int count;
  FaceBox faces[4];
};

void initFaceDetection();
FaceDetectionResult detectFaces(const uint8_t* jpeg_data, size_t length);
void getFaceDetectionDiagnosticsJson(char* buf, size_t max_len);
