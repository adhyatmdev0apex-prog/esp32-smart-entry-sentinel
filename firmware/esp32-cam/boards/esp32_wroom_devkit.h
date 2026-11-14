#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * BOARD PROFILE: Standard ESP32-WROOM-32 / DevKit / NodeMCU-32S
 * Hardware: Dual-Core ESP32-WROOM-32 (No Camera, No External PSRAM)
 * ============================================================================
 */

#define BOARD_NAME "ESP32-WROOM DevKit"
#define BOARD_HAS_CAMERA 0
#define BOARD_HAS_PSRAM  0

// --- Status Indicator ---
#define PIN_BOARD_LED        2  // Built-in blue LED on most DevKit boards (active HIGH)

// --- User Sensor Pin Defaults ---
// Standard WROOM boards have plenty of unconstrained GPIOs:
#define PIN_DEFAULT_REED    27  // Reed magnetic door sensor (internal pull-up)
#define PIN_DEFAULT_PIR     14  // PIR motion sensor (input)
#define PIN_DEFAULT_BUZZER  26  // Optional active or passive buzzer

// --- Actuators / Optional Outputs ---
#define PIN_FLASH_LED       -1
#define PIN_COOLING_FAN     -1
