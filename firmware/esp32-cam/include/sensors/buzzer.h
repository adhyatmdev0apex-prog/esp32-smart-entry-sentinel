#pragma once
#include <Arduino.h>

void initBuzzer();
void updateBuzzer();
void beep(uint16_t duration_ms = 100, uint8_t count = 1);
void playTwoToneAlert(uint16_t total_duration_ms = 3000);
