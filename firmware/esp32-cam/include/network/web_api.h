#pragma once
#include <Arduino.h>

void initWebApi();
void updateWebApi();

void setFlashBrightness(uint8_t level);
uint8_t getFlashBrightness();
