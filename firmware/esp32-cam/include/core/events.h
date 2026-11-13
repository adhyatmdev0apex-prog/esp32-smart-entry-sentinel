#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * COMMON EVENT SYSTEM
 * Standardized event model shared across WROOM, CAM, sensors, and network.
 * ============================================================================
 */

typedef enum {
  EVENT_NONE = 0,
  EVENT_BOOT,
  EVENT_WIFI_CONNECTED,
  EVENT_WIFI_DISCONNECTED,

  // Sensors & Door
  EVENT_DOOR_OPEN,
  EVENT_DOOR_CLOSED,
  EVENT_DOOR_TIMEOUT,
  EVENT_MOTION_START,
  EVENT_MOTION_END,

  // Camera & Streaming
  EVENT_CAMERA_STARTED,
  EVENT_CAMERA_STOPPED,
  EVENT_CAMERA_ERROR,
  EVENT_SNAPSHOT_CAPTURED,

  // Detection & AI (ESP32-side detection only)
  EVENT_FACE_FOUND,
  EVENT_FACE_LOST,
  EVENT_AI_REQUEST,
  EVENT_AI_RESULT,
  EVENT_AI_ERROR,

  // Storage
  EVENT_SD_MOUNTED,
  EVENT_SD_UNMOUNTED,
  EVENT_SD_ERROR,

  // System
  EVENT_OTA_START,
  EVENT_OTA_END,
  EVENT_CUSTOM,
  EVENT_SYSTEM_ERROR
} EventType;

struct Event {
  EventType type;
  char source[16];
  uint32_t timestamp_ms;
  int32_t value;
  char extra[32];
};

typedef void (*EventSubscriber)(const Event& event);

void initEventSystem();
void postEvent(EventType type, const char* source, int32_t value = 0, const char* extra = "");
bool registerEventSubscriber(EventSubscriber subscriber);
const char* getEventTypeName(EventType type);

// Event history for web API (ring buffer)
size_t getRecentEvents(Event* out_events, size_t max_count);
size_t getRecentEventsJson(char* buf, size_t max_len);
