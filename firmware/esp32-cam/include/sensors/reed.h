#pragma once
#include <Arduino.h>
#include "sensors/sensor_types.h"

void initReedSensor();
void updateReedSensor();

bool isDoorOpen();
ReedStatus getReedStatus();
void getReedDiagnosticsJson(char* buf, size_t max_len);
