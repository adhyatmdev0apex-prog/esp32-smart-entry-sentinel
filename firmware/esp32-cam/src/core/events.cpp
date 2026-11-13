#include "core/events.h"
#include <string.h>
#include <stdio.h>

#define MAX_SUBSCRIBERS 8
#define EVENT_HISTORY_SIZE 16

static EventSubscriber s_subscribers[MAX_SUBSCRIBERS];
static size_t s_subscriber_count = 0;

static Event s_history[EVENT_HISTORY_SIZE];
static size_t s_history_head = 0;
static size_t s_history_total = 0;

const char* getEventTypeName(EventType type) {
  switch (type) {
    case EVENT_BOOT:                return "BOOT";
    case EVENT_WIFI_CONNECTED:      return "WIFI_CONNECTED";
    case EVENT_WIFI_DISCONNECTED:   return "WIFI_DISCONNECTED";
    case EVENT_DOOR_OPEN:           return "DOOR_OPEN";
    case EVENT_DOOR_CLOSED:         return "DOOR_CLOSED";
    case EVENT_DOOR_TIMEOUT:        return "DOOR_TIMEOUT";
    case EVENT_MOTION_START:        return "MOTION_START";
    case EVENT_MOTION_END:          return "MOTION_END";
    case EVENT_CAMERA_STARTED:      return "CAMERA_STARTED";
    case EVENT_CAMERA_STOPPED:      return "CAMERA_STOPPED";
    case EVENT_CAMERA_ERROR:        return "CAMERA_ERROR";
    case EVENT_SNAPSHOT_CAPTURED:   return "SNAPSHOT_CAPTURED";
    case EVENT_FACE_FOUND:          return "FACE_FOUND";
    case EVENT_FACE_LOST:           return "FACE_LOST";
    case EVENT_AI_REQUEST:          return "AI_REQUEST";
    case EVENT_AI_RESULT:           return "AI_RESULT";
    case EVENT_AI_ERROR:            return "AI_ERROR";
    case EVENT_SD_MOUNTED:          return "SD_MOUNTED";
    case EVENT_SD_UNMOUNTED:        return "SD_UNMOUNTED";
    case EVENT_SD_ERROR:            return "SD_ERROR";
    case EVENT_OTA_START:           return "OTA_START";
    case EVENT_OTA_END:             return "OTA_END";
    case EVENT_CUSTOM:              return "CUSTOM";
    case EVENT_SYSTEM_ERROR:        return "SYSTEM_ERROR";
    default:                        return "UNKNOWN";
  }
}

void initEventSystem() {
  s_subscriber_count = 0;
  s_history_head = 0;
  s_history_total = 0;
  memset(s_history, 0, sizeof(s_history));
}

bool registerEventSubscriber(EventSubscriber subscriber) {
  if (s_subscriber_count >= MAX_SUBSCRIBERS || subscriber == nullptr) {
    return false;
  }
  s_subscribers[s_subscriber_count++] = subscriber;
  return true;
}

void postEvent(EventType type, const char* source, int32_t value, const char* extra) {
  Event ev;
  ev.type = type;
  ev.timestamp_ms = millis();
  ev.value = value;
  strncpy(ev.source, source ? source : "SYS", sizeof(ev.source) - 1);
  ev.source[sizeof(ev.source) - 1] = '\0';
  strncpy(ev.extra, extra ? extra : "", sizeof(ev.extra) - 1);
  ev.extra[sizeof(ev.extra) - 1] = '\0';

  // Store in circular history buffer
  s_history[s_history_head] = ev;
  s_history_head = (s_history_head + 1) % EVENT_HISTORY_SIZE;
  if (s_history_total < EVENT_HISTORY_SIZE) {
    s_history_total++;
  }

  // Notify registered subscribers
  for (size_t i = 0; i < s_subscriber_count; i++) {
    if (s_subscribers[i]) {
      s_subscribers[i](ev);
    }
  }
}

size_t getRecentEvents(Event* out_events, size_t max_count) {
  if (!out_events || max_count == 0) return 0;
  size_t count = (s_history_total < max_count) ? s_history_total : max_count;
  for (size_t i = 0; i < count; i++) {
    size_t idx = (s_history_head + EVENT_HISTORY_SIZE - 1 - i) % EVENT_HISTORY_SIZE;
    out_events[i] = s_history[idx];
  }
  return count;
}

size_t getRecentEventsJson(char* buf, size_t max_len) {
  if (!buf || max_len < 16) return 0;
  int written = snprintf(buf, max_len, "{\"count\":%u,\"events\":[", (unsigned int)s_history_total);
  if (written < 0 || (size_t)written >= max_len) return 0;

  size_t count = (s_history_total < 20) ? s_history_total : 20;
  for (size_t i = 0; i < count; i++) {
    size_t idx = (s_history_head + EVENT_HISTORY_SIZE - 1 - i) % EVENT_HISTORY_SIZE;
    const Event& ev = s_history[idx];
    int n = snprintf(buf + written, max_len - written,
                     "%s{\"type\":\"%s\",\"src\":\"%s\",\"time_ms\":%u,\"val\":%d,\"extra\":\"%s\"}",
                     (i > 0 ? "," : ""),
                     getEventTypeName(ev.type),
                     ev.source,
                     (unsigned int)ev.timestamp_ms,
                     (int)ev.value,
                     ev.extra);
    if (n < 0 || (size_t)(written + n) >= max_len - 4) break;
    written += n;
  }
  strcat(buf, "]}");
  return strlen(buf);
}
