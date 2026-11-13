#pragma once
#include <Arduino.h>
#include "core/events.h"

void initWroomCamLink();
void updateWroomCamLink();

// WROOM -> CAM: sends an event over Wi-Fi (e.g., DOOR_OPEN -> triggers CAM snapshot)
bool sendEventToPeer(const Event& event);
void getLinkDiagnosticsJson(char* buf, size_t max_len);
