#include "communication/wroom_cam_link.h"
#include "app_config.h"
#include "core/logger.h"
#include "network/wifi_manager.h"
#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <WiFiUdp.h>
#include <esp_now.h>
#include <FS.h>
#include <SPIFFS.h>

#if BOARD_HAS_CAMERA
#include "camera/camera_manager.h"
#include <esp_camera.h>
#endif

#if defined(SYSTEM_MODE_WROOM_CAM)
static uint32_t s_sent_count = 0;
static uint32_t s_fail_count = 0;
static QueueHandle_t s_link_queue = nullptr;

static bool s_espnow_ready = false;
static uint32_t s_last_err_bcast_ms = 0;
static const uint8_t BROADCAST_MAC[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const size_t ESPNOW_IMG_CHUNK = 220;
static const size_t MAX_SUPER_JPG_BYTES = 32768;

enum EspNowMsgType : uint8_t {
  ESPNOW_EVENT     = 1,
  ESPNOW_IMG_START = 2,
  ESPNOW_IMG_DATA  = 3,
  ESPNOW_IMG_END   = 4
};

struct __attribute__((packed)) EspNowEventPkt {
  uint8_t msg_type;   // ESPNOW_EVENT
  uint8_t event_type; // EventType or 250 = ERR_NO_WIFI
  int32_t value;
  uint32_t time_ms;
  char src[12];
};

struct __attribute__((packed)) EspNowImgStartPkt {
  uint8_t msg_type; // ESPNOW_IMG_START
  uint32_t total_len;
  uint16_t total_chunks;
};

struct __attribute__((packed)) EspNowImgDataPkt {
  uint8_t msg_type; // ESPNOW_IMG_DATA
  uint16_t chunk_idx;
  uint8_t data_len;
  uint8_t data[ESPNOW_IMG_CHUNK];
};

struct __attribute__((packed)) EspNowImgEndPkt {
  uint8_t msg_type; // ESPNOW_IMG_END
  uint32_t total_len;
};

static volatile bool s_peer_no_wifi_rx = false;

#if BOARD_HAS_CAMERA
static volatile bool s_espnow_door_open_rx = false;
static volatile bool s_espnow_door_closed_rx = false;
static volatile bool s_espnow_motion_rx = false;
static bool s_door_snap_pending = false;
static uint32_t s_door_snap_due_ms = 0;
#else
static bool s_wroom_spiffs_ready = false;
static uint8_t* s_rx_img_buf = nullptr;
static size_t s_rx_expected_len = 0;
static size_t s_rx_written_len = 0;
static volatile bool s_rx_img_ready = false;
#endif

static void onEspNowRecv(const uint8_t* mac, const uint8_t* data, int len) {
  (void)mac;
  if (!data || len < 1) return;
  uint8_t msg_type = data[0];

  if (msg_type == ESPNOW_EVENT && len >= (int)sizeof(EspNowEventPkt)) {
    const EspNowEventPkt* pkt = (const EspNowEventPkt*)data;
    if (pkt->event_type == 250) {
      s_peer_no_wifi_rx = true;
      return;
    }
#if BOARD_HAS_CAMERA
    if (pkt->event_type == (uint8_t)EVENT_DOOR_OPEN) {
      s_espnow_door_open_rx = true;
    } else if (pkt->event_type == (uint8_t)EVENT_DOOR_CLOSED) {
      s_espnow_door_closed_rx = true;
    } else if (pkt->event_type == (uint8_t)EVENT_MOTION_START) {
      s_espnow_motion_rx = true;
    }
#endif
  }
#if !BOARD_HAS_CAMERA
  else if (msg_type == ESPNOW_IMG_START && len >= (int)sizeof(EspNowImgStartPkt)) {
    const EspNowImgStartPkt* pkt = (const EspNowImgStartPkt*)data;
    if (pkt->total_len > 0 && pkt->total_len <= MAX_SUPER_JPG_BYTES) {
      if (!s_rx_img_buf) s_rx_img_buf = (uint8_t*)malloc(MAX_SUPER_JPG_BYTES);
      if (s_rx_img_buf) {
        s_rx_expected_len = pkt->total_len;
        s_rx_written_len = 0;
        s_rx_img_ready = false;
      }
    }
  } else if (msg_type == ESPNOW_IMG_DATA && len >= 4) {
    const EspNowImgDataPkt* pkt = (const EspNowImgDataPkt*)data;
    if (s_rx_img_buf && pkt->data_len <= ESPNOW_IMG_CHUNK) {
      size_t offset = (size_t)pkt->chunk_idx * ESPNOW_IMG_CHUNK;
      if (offset + pkt->data_len <= MAX_SUPER_JPG_BYTES) {
        memcpy(s_rx_img_buf + offset, pkt->data, pkt->data_len);
        if (offset + pkt->data_len > s_rx_written_len) {
          s_rx_written_len = offset + pkt->data_len;
        }
      }
    }
  } else if (msg_type == ESPNOW_IMG_END && len >= (int)sizeof(EspNowImgEndPkt)) {
    const EspNowImgEndPkt* pkt = (const EspNowImgEndPkt*)data;
    if (s_rx_img_buf && s_rx_written_len >= pkt->total_len && pkt->total_len > 0) {
      s_rx_expected_len = pkt->total_len;
      s_rx_img_ready = true;
    }
  }
#endif
}

static bool ensureEspNowInit() {
  if (s_espnow_ready) return true;
  if (esp_now_init() != ESP_OK) {
    logError("ESPNOW", "esp_now_init failed");
    return false;
  }
  esp_now_register_recv_cb(onEspNowRecv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST_MAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  if (!esp_now_is_peer_exist(BROADCAST_MAC)) {
    esp_now_add_peer(&peer);
  }
  s_espnow_ready = true;
  logInfo("ESPNOW", "ESP-NOW broadcast link initialized");
  return true;
}

static bool sendEspNowEvent(uint8_t ev_type, const char* src, int32_t val) {
  if (!ensureEspNowInit()) return false;
  EspNowEventPkt pkt = {};
  pkt.msg_type = ESPNOW_EVENT;
  pkt.event_type = ev_type;
  pkt.value = val;
  pkt.time_ms = millis();
  snprintf(pkt.src, sizeof(pkt.src), "%s", src ? src : "NODE");
  return (esp_now_send(BROADCAST_MAC, (const uint8_t*)&pkt, sizeof(pkt)) == ESP_OK);
}

#if !BOARD_HAS_CAMERA
bool saveDoorImageToSpiffs(const uint8_t* data, size_t len) {
  if (!data || len == 0) return false;
  if (!s_wroom_spiffs_ready) {
    s_wroom_spiffs_ready = SPIFFS.begin(true);
  }
  if (!s_wroom_spiffs_ready) return false;

  if (SPIFFS.exists("/door_last.jpg")) {
    SPIFFS.remove("/door_prev.jpg");
    SPIFFS.rename("/door_last.jpg", "/door_prev.jpg");
  }
  File f = SPIFFS.open("/door_last.jpg", FILE_WRITE);
  if (!f) return false;
  size_t written = f.write(data, len);
  f.close();
  logInfo("WROOM", "Saved 1.2s door image to SPIFFS (/door_last.jpg, %u bytes)", (unsigned)written);
  return (written == len);
}

static void wroomLinkWorkerTask(void* param) {
  Event ev;
  for (;;) {
    if (xQueueReceive(s_link_queue, &ev, portMAX_DELAY) == pdTRUE) {
      sendEventToPeer(ev);
    }
  }
}

static void onLocalEvent(const Event& event) {
  if (event.type == EVENT_DOOR_OPEN || event.type == EVENT_DOOR_CLOSED || event.type == EVENT_MOTION_START) {
    if (s_link_queue) {
      xQueueSend(s_link_queue, &event, 0);
    }
  }
}
#else
bool saveDoorImageToSpiffs(const uint8_t*, size_t) { return false; }

void scheduleDoorCapture1200ms() {
  s_door_snap_due_ms = millis() + 1200;
  s_door_snap_pending = true;
}

static void sendSuperCompressedImageToWroom(const uint8_t* jpg, size_t len) {
  if (!jpg || len == 0 || len > MAX_SUPER_JPG_BYTES) return;

  if (isWiFiConnected()) {
    WiFiClient client;
    HTTPClient http;
    char url[96];
    snprintf(url, sizeof(url), "http://%s:%d/api/v1/image", CFG_PEER_WROOM_IP, CFG_HTTP_PORT);
    http.setTimeout(1500);
    if (http.begin(client, url)) {
      http.addHeader("Content-Type", "image/jpeg");
      int code = http.POST((uint8_t*)jpg, len);
      http.end();
      if (code == HTTP_CODE_OK) {
        logInfo("LINK", "Sent 1.2s compressed image (%u bytes) to WROOM via Wi-Fi", (unsigned)len);
        return;
      }
    }
  }

  // Fallback or Wi-Fi unavailable: send over ESP-NOW chunks
  if (!ensureEspNowInit()) return;
  uint16_t total_chunks = (uint16_t)((len + ESPNOW_IMG_CHUNK - 1) / ESPNOW_IMG_CHUNK);
  EspNowImgStartPkt start_pkt = { ESPNOW_IMG_START, (uint32_t)len, total_chunks };
  esp_now_send(BROADCAST_MAC, (const uint8_t*)&start_pkt, sizeof(start_pkt));
  delay(4);

  for (uint16_t idx = 0; idx < total_chunks; idx++) {
    size_t offset = (size_t)idx * ESPNOW_IMG_CHUNK;
    size_t clen = len - offset;
    if (clen > ESPNOW_IMG_CHUNK) clen = ESPNOW_IMG_CHUNK;
    EspNowImgDataPkt dpkt = {};
    dpkt.msg_type = ESPNOW_IMG_DATA;
    dpkt.chunk_idx = idx;
    dpkt.data_len = (uint8_t)clen;
    memcpy(dpkt.data, jpg + offset, clen);
    esp_now_send(BROADCAST_MAC, (const uint8_t*)&dpkt, 4 + clen);
    delay(4);
  }

  EspNowImgEndPkt end_pkt = { ESPNOW_IMG_END, (uint32_t)len };
  esp_now_send(BROADCAST_MAC, (const uint8_t*)&end_pkt, sizeof(end_pkt));
  logInfo("ESPNOW", "Sent 1.2s compressed image (%u bytes, %u chunks) to WROOM", (unsigned)len, (unsigned)total_chunks);
}

static void captureAndSend1200msFrame() {
  if (!isCameraSensorActive() || !isCameraReady()) return;
  if (!takeCameraMutex(500)) return;

  sensor_t* s = esp_camera_sensor_get();
  framesize_t orig_fs = FRAMESIZE_SVGA;
  int orig_q = 12;
  if (s) {
    orig_fs = s->status.framesize;
    orig_q = s->status.quality;
    s->set_framesize(s, FRAMESIZE_QVGA);
    s->set_quality(s, 42);
  }

  // Flush 1 stale frame and grab super-compressed QVGA JPEG
  camera_fb_t* flush_fb = esp_camera_fb_get();
  if (flush_fb) esp_camera_fb_return(flush_fb);
  camera_fb_t* fb = esp_camera_fb_get();

  uint8_t* copy_buf = nullptr;
  size_t copy_len = 0;
  if (fb && fb->len > 0 && fb->len <= MAX_SUPER_JPG_BYTES) {
    copy_buf = (uint8_t*)malloc(fb->len);
    if (copy_buf) {
      memcpy(copy_buf, fb->buf, fb->len);
      copy_len = fb->len;
    }
  }
  if (fb) esp_camera_fb_return(fb);

  if (s) {
    s->set_framesize(s, orig_fs);
    s->set_quality(s, orig_q);
  }
  giveCameraMutex();

  if (copy_buf && copy_len > 0) {
    sendSuperCompressedImageToWroom(copy_buf, copy_len);
    free(copy_buf);
  }
}
#endif

void initWroomCamLink() {
#if !BOARD_HAS_CAMERA
  s_wroom_spiffs_ready = SPIFFS.begin(true);
  s_link_queue = xQueueCreate(16, sizeof(Event));
  if (s_link_queue) {
    xTaskCreatePinnedToCore(wroomLinkWorkerTask, "wroom_link", 4096, nullptr, 3, nullptr, 0);
  }
  registerEventSubscriber(onLocalEvent);
  logInfo("LINK", "WROOM -> CAM Async Link active (Wi-Fi + ESP-NOW fallback, SPIFFS=%s)",
          s_wroom_spiffs_ready ? "OK" : "ERR");
#else
  logInfo("LINK", "CAM <- WROOM Inter-device Link ready (Wi-Fi + ESP-NOW fallback)");
#endif
}

void updateWroomCamLink() {
  if (s_peer_no_wifi_rx) {
    s_peer_no_wifi_rx = false;
    logWarn("ESPNOW", "Peer reported ERR_NO_WIFI -> ESP-NOW peer communication active");
    postEvent(EVENT_CUSTOM, "ESPNOW", 250, "ERR_NO_WIFI");
  }

  if (isWifiFallbackActive()) {
    ensureEspNowInit();
    uint32_t now = millis();
    if (now - s_last_err_bcast_ms >= 15000) {
      s_last_err_bcast_ms = now;
      sendEspNowEvent(250, CFG_DEVICE_ID, 0); // ERR_NO_WIFI heartbeat
    }
  }

#if BOARD_HAS_CAMERA
  if (s_espnow_door_open_rx) {
    s_espnow_door_open_rx = false;
    setCameraSensorActive(true);
    scheduleDoorCapture1200ms();
    postEvent(EVENT_DOOR_OPEN, "ESPNOW");
  }
  if (s_espnow_motion_rx) {
    s_espnow_motion_rx = false;
    setCameraSensorActive(true);
    scheduleDoorCapture1200ms();
    postEvent(EVENT_MOTION_START, "ESPNOW");
  }
  if (s_espnow_door_closed_rx) {
    s_espnow_door_closed_rx = false;
    postEvent(EVENT_DOOR_CLOSED, "ESPNOW");
  }
  if (s_door_snap_pending && (int32_t)(millis() - s_door_snap_due_ms) >= 0) {
    s_door_snap_pending = false;
    captureAndSend1200msFrame();
  }
#else
  if (s_rx_img_ready && s_rx_img_buf && s_rx_expected_len > 0) {
    s_rx_img_ready = false;
    saveDoorImageToSpiffs(s_rx_img_buf, s_rx_expected_len);
  }
#endif
}

bool sendEventToPeer(const Event& event) {
  if (!isWiFiConnected()) {
    bool ok = sendEspNowEvent((uint8_t)event.type, event.source, event.value);
    if (ok) s_sent_count++; else s_fail_count++;
    return ok;
  }

  WiFiClient client;
  HTTPClient http;
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d/api/v1/event", CFG_PEER_CAM_IP, CFG_HTTP_PORT);

  http.setTimeout(CFG_LINK_TIMEOUT_MS);
  if (!http.begin(client, url)) {
    s_fail_count++;
    return sendEspNowEvent((uint8_t)event.type, event.source, event.value);
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

  WiFiUDP udp;
  if (udp.beginPacket(IPAddress(255, 255, 255, 255), 5055) == 1) {
    udp.print(payload);
    udp.endPacket();
  }

  if (code == HTTP_CODE_OK) {
    s_sent_count++;
    return true;
  }
  s_fail_count++;
  return sendEspNowEvent((uint8_t)event.type, event.source, event.value);
}

void getLinkDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"peer_link\":{\"sent\":%u,\"failed\":%u,\"espnow\":%s}",
           (unsigned int)s_sent_count, (unsigned int)s_fail_count,
           s_espnow_ready ? "true" : "false");
}

#else

void initWroomCamLink() {}
void updateWroomCamLink() {}
bool sendEventToPeer(const Event&) { return false; }
void scheduleDoorCapture1200ms() {}
bool saveDoorImageToSpiffs(const uint8_t*, size_t) { return false; }
void getLinkDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"peer_link\":{\"enabled\":false}");
}

#endif
