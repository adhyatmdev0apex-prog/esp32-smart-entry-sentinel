#pragma once
#include "version.h"
#include "configs.h"
#include "secrets.h"

// Include hardware board definitions
#include "boards.h"

/**
 * ============================================================================
 * INTERNAL CONFIGURATION RESOLUTION
 * Validates combinations, ensures safety defaults, and exposes boolean flags.
 * ============================================================================
 */

// Verify exactly one mode is selected:
#if defined(SYSTEM_MODE_WROOM_ONLY) && (defined(SYSTEM_MODE_CAM_ONLY) || defined(SYSTEM_MODE_WROOM_CAM))
  #error "Conflict: More than one SYSTEM_MODE selected in configs.h!"
#endif
#if !defined(SYSTEM_MODE_WROOM_ONLY) && !defined(SYSTEM_MODE_CAM_ONLY) && !defined(SYSTEM_MODE_WROOM_CAM)
  #define SYSTEM_MODE_CAM_ONLY // Default to CAM_ONLY if unset
#endif

#if defined(SYSTEM_MODE_WROOM_ONLY)
  #define SYSTEM_MODE_NAME "WROOM_ONLY"
#elif defined(SYSTEM_MODE_CAM_ONLY)
  #define SYSTEM_MODE_NAME "CAM_ONLY"
#elif defined(SYSTEM_MODE_WROOM_CAM)
  #define SYSTEM_MODE_NAME "WROOM_CAM"
#else
  #define SYSTEM_MODE_NAME "UNKNOWN"
#endif

// Capability resolution:
#if defined(SYSTEM_MODE_WROOM_ONLY)
  #define HAS_CAMERA_FEATURE  0
  #define HAS_COOLING_FEATURE 0
#else
  #define HAS_CAMERA_FEATURE  (BOARD_HAS_CAMERA && CFG_ENABLE_CAMERA)
  #define HAS_COOLING_FEATURE (BOARD_HAS_CAMERA && CFG_ENABLE_COOLING && (PIN_COOLING_FAN >= 0))
#endif

#define HAS_SENSOR_REED     (CFG_ENABLE_REED && (PIN_DEFAULT_REED >= 0))
#define HAS_SENSOR_PIR      (CFG_ENABLE_PIR && (PIN_DEFAULT_PIR >= 0))
#define HAS_BUZZER          (CFG_ENABLE_BUZZER && (PIN_DEFAULT_BUZZER >= 0))
#define HAS_SD_STORAGE      (CFG_ENABLE_SD_STORAGE && BOARD_HAS_CAMERA)
#define HAS_FACE_DETECTION  (HAS_CAMERA_FEATURE && CFG_ENABLE_FACE_DETECTION)
#define HAS_LOCAL_AI        (HAS_CAMERA_FEATURE && CFG_ENABLE_LOCAL_AI)
