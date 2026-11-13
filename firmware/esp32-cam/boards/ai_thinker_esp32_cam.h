#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * BOARD PROFILE: AI-Thinker ESP32-CAM
 * Hardware: ESP32-S + OV2640 + 4MB External PSRAM + MicroSD slot
 * ============================================================================
 */

#define BOARD_NAME "AI-Thinker ESP32-CAM"
#define BOARD_HAS_CAMERA 1
#define BOARD_HAS_PSRAM  1

// --- Camera Pin Definitions (OV2640 / DVP Interface) ---
#define PIN_CAM_PWDN        32
#define PIN_CAM_RESET       -1
#define PIN_CAM_XCLK         0
#define PIN_CAM_SIOD        26
#define PIN_CAM_SIOC        27

#define PIN_CAM_D7          35  // Y9
#define PIN_CAM_D6          34  // Y8
#define PIN_CAM_D5          39  // Y7
#define PIN_CAM_D4          36  // Y6
#define PIN_CAM_D3          21  // Y5
#define PIN_CAM_D2          19  // Y4
#define PIN_CAM_D1          18  // Y3
#define PIN_CAM_D0           5  // Y2

#define PIN_CAM_VSYNC       25
#define PIN_CAM_HREF        23
#define PIN_CAM_PCLK        22

// --- On-Board Actuators / Indicators ---
#define PIN_FLASH_LED        4  // High-power flash LED (active HIGH, PWM capable)
#define PIN_BOARD_LED       33  // Small red status LED on back (active LOW)

// --- Cooling Fan (LEDC PWM) ---
// Dedicated to GPIO 13 (IRFZ44N N-channel MOSFET low-side drive)
#define PIN_COOLING_FAN     13
#define COOLING_LEDC_CH      2  // LEDC Channel 2
#define COOLING_LEDC_TMR     1  // LEDC Timer 1 (Timer 0 is reserved for 24MHz Camera XCLK!)

// --- MicroSD Pins (HS2 Interface) ---
// Note: When SD is enabled, GPIO 12, 13, 14, 15, 2, 4 are reserved for SD bus.
#define PIN_SD_CLK          14
#define PIN_SD_CMD          15
#define PIN_SD_D0            2
#define PIN_SD_D1            4
#define PIN_SD_D2           12
#define PIN_SD_D3           13

// --- User Sensor Pins (when SD is NOT used) ---
// If MicroSD is disabled, GPIO 12 and 14 are available as input pins for Reed or PIR
#define PIN_DEFAULT_REED    14
#define PIN_DEFAULT_PIR     12
#define PIN_DEFAULT_BUZZER  -1
