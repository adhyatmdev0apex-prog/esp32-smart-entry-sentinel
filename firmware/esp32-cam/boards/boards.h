#pragma once

/**
 * ============================================================================
 * BOARD SELECTOR ROUTER
 * Resolves board-specific pinout and capability definitions.
 * ============================================================================
 */

#if defined(BOARD_ESP32_WROOM)
  #include "esp32_wroom_devkit.h"
#elif defined(BOARD_AI_THINKER_CAM) || defined(ARDUINO_ESP32_CAM) || !defined(BOARD_HAS_CAMERA)
  #include "ai_thinker_esp32_cam.h"
#else
  #error "Unknown or unsupported board profile! Please define BOARD_AI_THINKER_CAM or BOARD_ESP32_WROOM."
#endif
