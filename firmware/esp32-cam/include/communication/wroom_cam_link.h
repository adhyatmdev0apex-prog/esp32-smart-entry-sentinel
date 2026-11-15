#pragma once
#include <Arduino.h>
#include "core/events.h"

void initWroomCamLink();
void updateWroomCamLink();

// WROOM -> CAM: sends an event over Wi-Fi or ESP-NOW fallback
bool sendEventToPeer(const Event& event);
void scheduleDoorCapture1200ms();
void scheduleAutoStandby(uint32_t delay_ms);
void cancelAutoStandby();
bool saveDoorImageToSpiffs(const uint8_t* data, size_t len);
void getLinkDiagnosticsJson(char* buf, size_t max_len);
