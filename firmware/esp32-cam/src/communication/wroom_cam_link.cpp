#include "communication/wroom_cam_link.h"
#include "app_config.h"
#include "core/logger.h"
#include "network/wifi_manager.h"
#include <WiFiClient.h>
#include <HTTPClient.h>

#if defined(SYSTEM_MODE_WROOM_CAM)
static uint32_t s_sent_count = 0;
static uint32_t s_fail_count = 0;
static QueueHandle_t s_link_queue = nullptr;

static void wroomLinkWorkerTask(void* param) {
  Event ev;
  for (;;) {
    if (xQueueReceive(s_link_queue, &ev, portMAX_DELAY) == pdTRUE) {
      sendEventToPeer(ev);
    }
  }
}

static void onLocalEvent(const Event& event) {
  // Queue immediately (non-blocking) so sensors & buzzer never stall on HTTP
  if (event.type == EVENT_DOOR_OPEN || event.type == EVENT_MOTION_START) {
    if (s_link_queue) {
      xQueueSend(s_link_queue, &event, 0);
    }
  }
}

void initWroomCamLink() {
#if !BOARD_HAS_CAMERA
  s_link_queue = xQueueCreate(16, sizeof(Event));
  if (s_link_queue) {
    xTaskCreatePinnedToCore(wroomLinkWorkerTask, "wroom_link", 4096, nullptr, 1, nullptr, 0);
  }
  registerEventSubscriber(onLocalEvent);
  logInfo("LINK", "WROOM -> CAM Async Link active on Core 0 (Peer target: %s)", CFG_PEER_CAM_IP);
#else
  logInfo("LINK", "CAM <- WROOM Inter-device Link ready (Listening for peer: %s)", CFG_PEER_WROOM_IP);
#endif
}

void updateWroomCamLink() {}

bool sendEventToPeer(const Event& event) {
  if (!isWiFiConnected()) return false;

  WiFiClient client;
  HTTPClient http;
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d/api/v1/event", CFG_PEER_CAM_IP, CFG_HTTP_PORT);

  http.setTimeout(CFG_LINK_TIMEOUT_MS);
  if (!http.begin(client, url)) {
    s_fail_count++;
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  char payload[160];
  snprintf(payload, sizeof(payload),
           "{\"event\":\"%s\",\"src\":\"%s\",\"val\":%d,\"time_ms\":%u}",
           getEventTypeName(event.type),
           event.source,
           (int)event.value,
           (unsigned int)event.timestamp_ms);

  int code = http.POST(payload);
  http.end();

  if (code == HTTP_CODE_OK) {
    s_sent_count++;
    return true;
  }
  s_fail_count++;
  return false;
}

void getLinkDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"peer_link\":{\"sent\":%u,\"failed\":%u}",
           (unsigned int)s_sent_count, (unsigned int)s_fail_count);
}

#else

void initWroomCamLink() {}
void updateWroomCamLink() {}
bool sendEventToPeer(const Event&) { return false; }
void getLinkDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"peer_link\":{\"enabled\":false}");
}

#endif
