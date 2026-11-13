#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * LOCAL / SERVER AI CLIENT
 * Offloads heavy vision tasks (YOLO, Face Recognition) to laptop/server.
 * Standard HTTP POST /api/v1/analyze returning standardized JSON.
 * ============================================================================
 */

struct AiAnalysisResult {
  bool success;
  bool person_detected;
  int person_count;
  int faces_detected;
  char label[32];
  float confidence;
};

void initLocalAiClient();
AiAnalysisResult sendSnapshotToAiServer(const uint8_t* jpeg_data, size_t length, const char* event_name);
void getLocalAiDiagnosticsJson(char* buf, size_t max_len);
