#pragma once
#include <Arduino.h>

struct ReedStatus {
  bool enabled;
  bool is_open;
  uint32_t last_change_ms;
  uint32_t open_duration_s;
  uint32_t open_count;
};

struct PirStatus {
  bool enabled;
  bool is_motion;
  bool is_warmed_up;
  uint32_t last_trigger_ms;
  uint32_t trigger_count;
};
