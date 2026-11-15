#include "sensors/pir.h"
#include "sensors/reed.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"
#include <stdio.h>

#if HAS_SENSOR_PIR
static int s_pir_pin = PIN_DEFAULT_PIR;
static bool s_is_motion = false;
static uint32_t s_boot_ms = 0;
static uint32_t s_last_trigger_ms = 0;
static uint32_t s_trigger_count = 0;

void initPirSensor() {
  pinMode(s_pir_pin, INPUT);
  s_boot_ms = millis();
  s_is_motion = false;
  logInfo("PIR", "Initialized on GPIO %d (Warming up for %u sec...)", s_pir_pin, CFG_PIR_WARMUP_MS / 1000);
}

void updatePirSensor() {
  // PIR only activates while the reed switch senses the door is OPEN
  if (!isDoorOpen()) {
    s_is_motion = false;
    return;
  }

  uint32_t now = millis();
  if (now - s_boot_ms < CFG_PIR_WARMUP_MS) {
    return; // Warmup guard
  }

  int raw = digitalRead(s_pir_pin);
  bool motion_active = (raw == CFG_PIR_ACTIVE_LEVEL);

  if (motion_active && !s_is_motion) {
    if (now - s_last_trigger_ms >= CFG_PIR_COOLDOWN_MS) {
      s_is_motion = true;
      s_last_trigger_ms = now;
      s_trigger_count++;
      logInfo("PIR", "Motion DETECTED (count: %u)", s_trigger_count);
      postEvent(EVENT_MOTION_START, "PIR", (int32_t)s_trigger_count);
    }
  } else if (!motion_active && s_is_motion) {
    s_is_motion = false;
    logInfo("PIR", "Motion ENDED");
    postEvent(EVENT_MOTION_END, "PIR");
  }
}

bool isMotionDetected() {
  return s_is_motion;
}

PirStatus getPirStatus() {
  PirStatus st;
  st.enabled = true;
  st.is_motion = s_is_motion;
  st.is_warmed_up = (millis() - s_boot_ms >= CFG_PIR_WARMUP_MS);
  st.last_trigger_ms = s_last_trigger_ms;
  st.trigger_count = s_trigger_count;
  return st;
}

void getPirDiagnosticsJson(char* buf, size_t max_len) {
  PirStatus st = getPirStatus();
  snprintf(buf, max_len,
           "\"pir\":{\"enabled\":true,\"gpio\":%d,\"is_motion\":%s,\"warmed_up\":%s,\"trigger_count\":%u}",
           s_pir_pin,
           st.is_motion ? "true" : "false",
           st.is_warmed_up ? "true" : "false",
           (unsigned int)st.trigger_count);
}

#else

void initPirSensor() {}
void updatePirSensor() {}
bool isMotionDetected() { return false; }
PirStatus getPirStatus() { return PirStatus{false, false, false, 0, 0}; }
void getPirDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"pir\":{\"enabled\":false}");
}
#endif
