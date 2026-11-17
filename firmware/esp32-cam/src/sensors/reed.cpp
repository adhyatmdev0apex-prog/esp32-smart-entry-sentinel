#include "sensors/reed.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"
#include <stdio.h>

#if HAS_SENSOR_REED
static int s_reed_pin = PIN_DEFAULT_REED;
static bool s_is_open = false;
static int s_last_raw_state = -1;
static uint32_t s_last_debounce_ms = 0;
static uint32_t s_last_state_change_ms = 0;
static uint32_t s_open_count = 0;
static bool s_timeout_reported = false;
static TaskHandle_t s_reed_notify_task = nullptr;
static uint32_t s_chatter_win_start_ms = 0;
static uint16_t s_chatter_edges = 0;
static bool s_chatter_reported = false;

static void IRAM_ATTR onReedIsr() {
  BaseType_t woken = pdFALSE;
  if (s_reed_notify_task != nullptr) {
    vTaskNotifyGiveFromISR(s_reed_notify_task, &woken);
  }
  if (woken) {
    portYIELD_FROM_ISR();
  }
}

void setReedNotifyTask(void* task_handle) {
  s_reed_notify_task = (TaskHandle_t)task_handle;
}

void initReedSensor() {
  pinMode(s_reed_pin, INPUT_PULLUP);
  int raw = digitalRead(s_reed_pin);
  s_is_open = (raw != CFG_REED_ACTIVE_LEVEL);
  s_last_raw_state = raw;
  s_last_debounce_ms = millis();
  s_last_state_change_ms = millis();
  attachInterrupt(digitalPinToInterrupt(s_reed_pin), onReedIsr, CHANGE);
  logInfo("REED", "Initialized on GPIO %d with ISR wakeup (Door state: %s)", s_reed_pin, s_is_open ? "OPEN" : "CLOSED");
}

void updateReedSensor() {
  int raw = digitalRead(s_reed_pin);
  uint32_t now = millis();
  bool current_open = (raw != CFG_REED_ACTIVE_LEVEL);

  // Leading-edge instant trigger for DOOR_OPEN (zero debounce wait on first open edge)
  if (current_open && !s_is_open && (now - s_last_state_change_ms) >= CFG_REED_DEBOUNCE_MS) {
    s_is_open = true;
    s_last_raw_state = raw;
    s_last_debounce_ms = now;
    s_last_state_change_ms = now;
    s_timeout_reported = false;
    s_open_count++;
    logInfo("REED", "Door OPENED instant (count: %u)", s_open_count);
    postEvent(EVENT_DOOR_OPEN, "REED", (int32_t)s_open_count);
  } else {
    if (raw != s_last_raw_state) {
      s_last_raw_state = raw;
      s_last_debounce_ms = now;
      if (now - s_chatter_win_start_ms > 3000) {
        s_chatter_win_start_ms = now;
        s_chatter_edges = 1;
        s_chatter_reported = false;
      } else if (++s_chatter_edges > 8 && !s_chatter_reported) {
        s_chatter_reported = true;
        logWarn("REED", "Chatter/tamper detected (%u edges in 3s)", (unsigned)s_chatter_edges);
        postEvent(EVENT_CUSTOM, "REED", (int32_t)s_chatter_edges, "TAMPER_CHATTER");
      }
    }

    if ((now - s_last_debounce_ms) >= CFG_REED_DEBOUNCE_MS) {
      if (current_open != s_is_open) {
        s_is_open = current_open;
        uint32_t prev_change = s_last_state_change_ms;
        s_last_state_change_ms = now;
        s_timeout_reported = false;

        if (s_is_open) {
          s_open_count++;
          logInfo("REED", "Door OPENED (count: %u)", s_open_count);
          postEvent(EVENT_DOOR_OPEN, "REED", (int32_t)s_open_count);
        } else {
          uint32_t duration_s = (now - prev_change) / 1000;
          logInfo("REED", "Door CLOSED (was open for %u sec)", duration_s);
          postEvent(EVENT_DOOR_CLOSED, "REED", (int32_t)duration_s);
        }
      }
    }
  }

  // Check door open timeout alert
  if (s_is_open && !s_timeout_reported && (now - s_last_state_change_ms >= CFG_REED_OPEN_TIMEOUT_MS)) {
    s_timeout_reported = true;
    logWarn("REED", "Door open timeout reached (> %u sec)!", CFG_REED_OPEN_TIMEOUT_MS / 1000);
    postEvent(EVENT_DOOR_TIMEOUT, "REED", (int32_t)(CFG_REED_OPEN_TIMEOUT_MS / 1000));
  }
}

bool isDoorOpen() {
  return s_is_open;
}

ReedStatus getReedStatus() {
  ReedStatus st;
  st.enabled = true;
  st.is_open = s_is_open;
  st.last_change_ms = s_last_state_change_ms;
  st.open_duration_s = s_is_open ? ((millis() - s_last_state_change_ms) / 1000) : 0;
  st.open_count = s_open_count;
  return st;
}

void getReedDiagnosticsJson(char* buf, size_t max_len) {
  ReedStatus st = getReedStatus();
  snprintf(buf, max_len,
           "\"reed\":{\"enabled\":true,\"gpio\":%d,\"is_open\":%s,\"open_duration_s\":%u,\"open_count\":%u}",
           s_reed_pin,
           st.is_open ? "true" : "false",
           (unsigned int)st.open_duration_s,
           (unsigned int)st.open_count);
}

#else

void initReedSensor() {}
void updateReedSensor() {}
bool isDoorOpen() { return false; }
ReedStatus getReedStatus() { return ReedStatus{false, false, 0, 0, 0}; }
void getReedDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"reed\":{\"enabled\":false}");
}
#endif
