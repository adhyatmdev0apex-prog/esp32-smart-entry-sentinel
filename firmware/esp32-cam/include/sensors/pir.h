#pragma once
#include <Arduino.h>
#include "sensors/sensor_types.h"

void initPirSensor();
void updatePirSensor();

bool isMotionDetected();
PirStatus getPirStatus();
void getPirDiagnosticsJson(char* buf, size_t max_len);
