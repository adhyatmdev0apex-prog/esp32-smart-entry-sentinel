#include "network/web_api.h"
#include "network/wifi_manager.h"
#include "core/events.h"
#include "core/logger.h"
#include "core/system.h"
#include "sensors/reed.h"
#include "sensors/pir.h"
#include "camera/camera_manager.h"
#include "camera/streaming.h"
#include "camera/cooling.h"
#include "communication/wroom_cam_link.h"
#include "app_config.h"

#include <esp_http_server.h>
#include <ArduinoOTA.h>
#include <FS.h>
#include <SPIFFS.h>

static httpd_handle_t s_httpd = NULL;
static uint8_t s_flash_level = 0;
static bool s_flash_inited = false;

void setFlashBrightness(uint8_t level) {
#if HAS_CAMERA_FEATURE && (PIN_FLASH_LED >= 0)
  s_flash_level = level;
  if (s_flash_level > 0) {
    if (!s_flash_inited) {
      ledcSetup(7, 5000, 8); // Channel 7, Timer 3
      ledcAttachPin(PIN_FLASH_LED, 7);
      s_flash_inited = true;
    }
    ledcWrite(7, s_flash_level);
  } else {
    if (s_flash_inited) {
      ledcWrite(7, 0);
    } else {
      pinMode(PIN_FLASH_LED, OUTPUT);
      digitalWrite(PIN_FLASH_LED, LOW);
    }
  }
#endif
}

uint8_t getFlashBrightness() {
  return s_flash_level;
}

static esp_err_t ping_handler(httpd_req_t* req) {
  httpd_resp_set_type(req, "text/plain");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "ESP32-CAM OK", 12);
}

static esp_err_t reboot_handler(httpd_req_t* req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_send(req, "Rebooting...", 12);
  requestReboot(400);
  return ESP_OK;
}

static esp_err_t status_handler(httpd_req_t* req) {
  notifyServerHeartbeat();
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  static char sys_diag[224];
  getSystemDiagnosticsJson(sys_diag, sizeof(sys_diag));

  static char wifi_diag[128];
  getWiFiDiagnosticsJson(wifi_diag, sizeof(wifi_diag));

  static char reed_diag[128];
  getReedDiagnosticsJson(reed_diag, sizeof(reed_diag));

  static char pir_diag[128];
  getPirDiagnosticsJson(pir_diag, sizeof(pir_diag));

#if HAS_CAMERA_FEATURE
  static char cam_diag[256];
  getCameraDiagnosticsJson(cam_diag, sizeof(cam_diag));

  static char stream_diag[160];
  getStreamingDiagnosticsJson(stream_diag, sizeof(stream_diag));

  static char cool_diag[256];
  getCoolingDiagnostics(cool_diag, sizeof(cool_diag));

  sensor_t* s = esp_camera_sensor_get();
  static char buf[1536];
  int len = snprintf(buf, sizeof(buf),
    "{\"status\":\"ok\",\"schema\":1,\"fw_version\":\"%s\",\"device\":\"%s\",\"mode\":\"%s\",%s,%s,%s,%s,%s,%s,%s,"
    "\"framesize\":%d,\"quality\":%d,\"flash\":%u}",
    FRAMEWORK_VERSION,
    CFG_DEVICE_ID,
    SYSTEM_MODE_NAME,
    sys_diag,
    wifi_diag,
    reed_diag,
    pir_diag,
    cam_diag,
    stream_diag,
    cool_diag,
    s ? (int)s->status.framesize : -1,
    s ? (int)s->status.quality : -1,
    (unsigned int)s_flash_level);
#else
  static char buf[1024];
  int len = snprintf(buf, sizeof(buf),
    "{\"status\":\"ok\",\"schema\":1,\"fw_version\":\"%s\",\"device\":\"%s\",\"mode\":\"%s\",%s,%s,%s,%s}",
    FRAMEWORK_VERSION,
    CFG_DEVICE_ID,
    SYSTEM_MODE_NAME,
    sys_diag,
    wifi_diag,
    reed_diag,
    pir_diag);
#endif

  return httpd_resp_send(req, buf, len);
}

static esp_err_t events_handler(httpd_req_t* req) {
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  static char buf[2048];
  size_t len = getRecentEventsJson(buf, sizeof(buf));
  return httpd_resp_send(req, buf, len);
}

static esp_err_t parse_get_query(httpd_req_t* req, char** obuf) {
  size_t buf_len = httpd_req_get_url_query_len(req) + 1;
  if (buf_len > 1) {
    char* buf = (char*)malloc(buf_len);
    if (!buf) {
      httpd_resp_send_500(req);
      return ESP_FAIL;
    }
    if (httpd_req_get_url_query_str(req, buf, buf_len) == ESP_OK) {
      *obuf = buf;
      return ESP_OK;
    }
    free(buf);
  }
  httpd_resp_send_404(req);
  return ESP_FAIL;
}

static esp_err_t control_handler(httpd_req_t* req) {
  char* qbuf = NULL;
  char variable[32];
  char value[32];

  if (parse_get_query(req, &qbuf) != ESP_OK) return ESP_FAIL;
  if (httpd_query_key_value(qbuf, "var", variable, sizeof(variable)) != ESP_OK ||
      httpd_query_key_value(qbuf, "val", value, sizeof(value)) != ESP_OK) {
    free(qbuf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(qbuf);

  int val = atoi(value);
  logInfo("HTTP", "Control request: %s = %d", variable, val);

  if (!strcmp(variable, "flash") || !strcmp(variable, "led_intensity")) {
    setFlashBrightness((uint8_t)val);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, "OK", 2);
  }

  if (!strcmp(variable, "txpower")) {
    setWiFiTxPower(val);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, "OK", 2);
  }

  if (!strcmp(variable, "wifi_proto")) {
    setWiFiProtocol(val);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, "OK", 2);
  }

#if HAS_CAMERA_FEATURE
  if (!strcmp(variable, "fan_ref_temp")) {
    setCoolingReferenceTemp((float)val);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, "OK", 2);
  }

  if (!strcmp(variable, "fan_xga_thresh")) {
    setCoolingXgaThreshold((float)val);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, "OK", 2);
  }

  if (!strcmp(variable, "fan_freq")) {
    setCoolingPwmFrequency((uint32_t)val);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, "OK", 2);
  }

  if (!strcmp(variable, "cam_sensor")) {
    bool ok = setCameraSensorActive(val != 0);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return ok ? httpd_resp_send(req, "OK", 2) : httpd_resp_send_500(req);
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    int res = 0;
    bool handled = true;
    takeCameraMutex(1000);
    if (!strcmp(variable, "framesize")) {
      res = s->set_framesize(s, (framesize_t)val);
      if (res == 0) notifyResolutionChange((framesize_t)val);
    } else if (!strcmp(variable, "quality")) {
      res = s->set_quality(s, val);
    } else if (!strcmp(variable, "brightness")) {
      res = s->set_brightness(s, val);
    } else if (!strcmp(variable, "contrast")) {
      res = s->set_contrast(s, val);
    } else if (!strcmp(variable, "saturation")) {
      res = s->set_saturation(s, val);
    } else if (!strcmp(variable, "special_effect")) {
      res = s->set_special_effect(s, val);
    } else if (!strcmp(variable, "vflip")) {
      res = s->set_vflip(s, val);
    } else if (!strcmp(variable, "hmirror")) {
      res = s->set_hmirror(s, val);
    } else if (!strcmp(variable, "awb")) {
      res = s->set_whitebal(s, val);
    } else if (!strcmp(variable, "aec")) {
      res = s->set_exposure_ctrl(s, val);
    } else {
      handled = false;
    }
    giveCameraMutex();

    if (handled) {
      httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
      if (res != 0) {
        return httpd_resp_send_500(req);
      }
      return httpd_resp_send(req, "OK", 2);
    }
  }
#endif

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "OK", 2);
}

#if HAS_CAMERA_FEATURE
static esp_err_t capture_handler(httpd_req_t* req) {
  if (!isCameraSensorActive()) {
    setCameraSensorActive(true);
  }
  size_t max_cap = psramFound() ? 131072 : 32768;
  uint8_t* snap_buf = (uint8_t*)heap_caps_malloc(max_cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!snap_buf) {
    snap_buf = (uint8_t*)malloc(32768);
    max_cap = 32768;
  }
  if (!snap_buf) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  size_t snap_len = 0;

  // Try fast copy from streaming staging buffer (<1ms, no camera mutex lock!)
  if (!copyLatestStreamFrame(snap_buf, max_cap, &snap_len)) {
    camera_fb_t* fb = captureSnapshotFb();
    if (!fb) {
      free(snap_buf);
      httpd_resp_send_500(req);
      return ESP_FAIL;
    }
    if (fb->len <= max_cap) {
      memcpy(snap_buf, fb->buf, fb->len);
      snap_len = fb->len;
    }
    returnSnapshotFb(fb);
  }

  if (snap_len == 0) {
    free(snap_buf);
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  // Chunked DRAM send
  size_t offset = 0;
  esp_err_t res = ESP_OK;
  while (offset < snap_len) {
    size_t chunk = snap_len - offset;
    if (chunk > 1460) chunk = 1460;
    res = httpd_resp_send_chunk(req, (const char*)(snap_buf + offset), chunk);
    if (res != ESP_OK) break;
    offset += chunk;
  }
  if (res == ESP_OK) {
    res = httpd_resp_send_chunk(req, NULL, 0);
  }
  free(snap_buf);
  return res;
}
#endif

static esp_err_t event_post_handler(httpd_req_t* req) {
  char buf[256];
  int ret = 0;
  int remaining = req->content_len;
  if (remaining >= (int)sizeof(buf)) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  if (remaining > 0) {
    ret = httpd_req_recv(req, buf, remaining);
    if (ret <= 0) {
      if (ret == HTTPD_SOCK_ERR_TIMEOUT) {
        httpd_resp_send_408(req);
      }
      return ESP_FAIL;
    }
    buf[ret] = '\0';
  } else {
    buf[0] = '\0';
  }

  logInfo("HTTP", "Received peer event: %s", buf);

  if (strstr(buf, "DOOR_OPEN")) {
#if HAS_CAMERA_FEATURE
    requestCameraWakeAsync();
    scheduleDoorCapture1200ms();
    scheduleAutoStandby(30000);
#endif
    postEvent(EVENT_DOOR_OPEN, "PEER");
  } else if (strstr(buf, "DOOR_CLOSED")) {
#if HAS_CAMERA_FEATURE
    scheduleAutoStandby(8000);
#endif
    postEvent(EVENT_DOOR_CLOSED, "PEER");
  } else if (strstr(buf, "MOTION_START")) {
    postEvent(EVENT_MOTION_START, "PEER");
  } else if (strstr(buf, "MOTION_END")) {
    postEvent(EVENT_MOTION_END, "PEER");
  } else {
    postEvent(EVENT_CUSTOM, "PEER");
  }

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  const char* resp = "{\"ok\":true,\"action\":\"event_received\"}";
  return httpd_resp_send(req, resp, strlen(resp));
}

#if HAS_CAMERA_FEATURE
static esp_err_t camera_power_handler(httpd_req_t* req) {
  bool target_active = !isCameraSensorActive();
  char* qbuf = NULL;
  if (httpd_req_get_url_query_len(req) > 0 && parse_get_query(req, &qbuf) == ESP_OK) {
    char state[16] = {0};
    if (httpd_query_key_value(qbuf, "state", state, sizeof(state)) == ESP_OK) {
      if (!strcmp(state, "on") || !strcmp(state, "1") || !strcmp(state, "true")) target_active = true;
      else if (!strcmp(state, "off") || !strcmp(state, "0") || !strcmp(state, "false")) target_active = false;
    }
    free(qbuf);
  } else if (req->content_len > 0 && req->content_len < 128) {
    char body[128] = {0};
    int r = httpd_req_recv(req, body, sizeof(body) - 1);
    if (r > 0) {
      body[r] = '\0';
      if (strstr(body, "true") || strstr(body, "\"on\"") || strstr(body, ":1")) target_active = true;
      else if (strstr(body, "false") || strstr(body, "\"off\"") || strstr(body, ":0")) target_active = false;
    }
  }

  notifyServerHeartbeat();
  if (target_active) cancelAutoStandby();
  bool ok = setCameraSensorActive(target_active);
  char resp[128];
  int len = snprintf(resp, sizeof(resp),
                     "{\"ok\":%s,\"sensor_active\":%s,\"ready\":%s}",
                     ok ? "true" : "false",
                     isCameraSensorActive() ? "true" : "false",
                     isCameraReady() ? "true" : "false");
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, resp, len);
}

static const char FALLBACK_DASHBOARD_HTML[] PROGMEM =
  "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
  "<title>Sentinel Fallback</title><style>body{font-family:sans-serif;background:#0f172a;color:#e2e8f0;margin:16px}"
  "button{padding:8px 14px;border:0;border-radius:6px;margin-right:8px;cursor:pointer;font-weight:700}"
  ".on{background:#10b981;color:#fff}.off{background:#475569;color:#fff}img{max-width:100%;border-radius:8px;background:#000}"
  "pre{background:#1e293b;padding:10px;border-radius:8px;overflow:auto;font-size:12px}</style></head><body>"
  "<h3>Sentinel Fallback Dashboard</h3><div>"
  "<button class='on' onclick=\"fetch('/api/v1/camera/power?state=on').then(r=>r.json()).then(()=>setTimeout(ls,200))\">Low-Power OFF (Live)</button>"
  "<button class='off' onclick=\"fetch('/api/v1/camera/power?state=off')\">Low-Power ON</button>"
  "</div><p><img id='v' alt='Camera Standby'></p><h4>Recent Events</h4><pre id='e'>Loading...</pre>"
  "<script>function ls(){document.getElementById('v').src='http://'+location.hostname+':81/stream?t='+Date.now()}"
  "function le(){fetch('/events').then(r=>r.json()).then(d=>{document.getElementById('e').textContent="
  "(d.events||[]).slice(0,12).map(x=>x.type+' ['+x.src+'] '+x.time_ms+'ms').join('\\n')||'No events'}).catch(()=>{})}"
  "ls();le();setInterval(le,2000);</script></body></html>";

static esp_err_t fallback_dashboard_handler(httpd_req_t* req) {
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, FALLBACK_DASHBOARD_HTML, strlen_P(FALLBACK_DASHBOARD_HTML));
}

static esp_err_t logs_handler(httpd_req_t* req) {
  char* buf = (char*)malloc(4096);
  if (!buf) return httpd_resp_send_500(req);
  size_t len = readSpiffsLogs(buf, 4096);
  httpd_resp_set_type(req, "text/plain");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t err = httpd_resp_send(req, len > 0 ? buf : "No logs yet", len > 0 ? len : 11);
  free(buf);
  return err;
}
#else
static esp_err_t image_post_handler(httpd_req_t* req) {
  if (req->content_len <= 0 || req->content_len > 32768) {
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  uint8_t* buf = (uint8_t*)malloc(req->content_len);
  if (!buf) return httpd_resp_send_500(req);
  int received = 0;
  while (received < (int)req->content_len) {
    int r = httpd_req_recv(req, (char*)buf + received, req->content_len - received);
    if (r <= 0) {
      free(buf);
      return httpd_resp_send_500(req);
    }
    received += r;
  }
  bool ok = saveDoorImageToSpiffs(buf, received);
  free(buf);
  httpd_resp_set_type(req, "application/json");
  return httpd_resp_send(req, ok ? "{\"ok\":true}" : "{\"ok\":false}", ok ? 11 : 12);
}

static esp_err_t image_get_handler(httpd_req_t* req) {
  if (!SPIFFS.exists("/door_last.jpg")) {
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  File f = SPIFFS.open("/door_last.jpg", FILE_READ);
  if (!f) return httpd_resp_send_500(req);
  size_t sz = f.size();
  uint8_t* buf = (uint8_t*)malloc(sz);
  if (!buf) { f.close(); return httpd_resp_send_500(req); }
  f.read(buf, sz);
  f.close();
  httpd_resp_set_type(req, "image/jpeg");
  esp_err_t err = httpd_resp_send(req, (const char*)buf, sz);
  free(buf);
  return err;
}
#endif

void initWebApi() {
  if (s_httpd == NULL) {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = CFG_HTTP_PORT;
    config.core_id = 1;
    config.stack_size = 8192;
    config.max_uri_handlers = 20;
    config.lru_purge_enable = true;

    static const httpd_uri_t uri_ping        = { .uri = "/ping",           .method = HTTP_GET,  .handler = ping_handler,       .user_ctx = NULL };
    static const httpd_uri_t uri_reboot      = { .uri = "/reboot",         .method = HTTP_GET,  .handler = reboot_handler,     .user_ctx = NULL };
    static const httpd_uri_t uri_status      = { .uri = "/status",         .method = HTTP_GET,  .handler = status_handler,     .user_ctx = NULL };
    static const httpd_uri_t uri_status2     = { .uri = "/api/v1/status",  .method = HTTP_GET,  .handler = status_handler,     .user_ctx = NULL };
    static const httpd_uri_t uri_events      = { .uri = "/events",         .method = HTTP_GET,  .handler = events_handler,     .user_ctx = NULL };
    static const httpd_uri_t uri_events2     = { .uri = "/api/v1/events",  .method = HTTP_GET,  .handler = events_handler,     .user_ctx = NULL };
    static const httpd_uri_t uri_control     = { .uri = "/control",        .method = HTTP_GET,  .handler = control_handler,    .user_ctx = NULL };
    static const httpd_uri_t uri_post_event  = { .uri = "/api/v1/event",   .method = HTTP_POST, .handler = event_post_handler, .user_ctx = NULL };
    static const httpd_uri_t uri_post_event2 = { .uri = "/event",          .method = HTTP_POST, .handler = event_post_handler, .user_ctx = NULL };

#if HAS_CAMERA_FEATURE
    static const httpd_uri_t uri_root        = { .uri = "/",                    .method = HTTP_GET,  .handler = fallback_dashboard_handler, .user_ctx = NULL };
    static const httpd_uri_t uri_logs        = { .uri = "/logs",                .method = HTTP_GET,  .handler = logs_handler,               .user_ctx = NULL };
    static const httpd_uri_t uri_logs2       = { .uri = "/api/v1/logs",         .method = HTTP_GET,  .handler = logs_handler,               .user_ctx = NULL };
    static const httpd_uri_t uri_capture     = { .uri = "/capture",             .method = HTTP_GET,  .handler = capture_handler,            .user_ctx = NULL };
    static const httpd_uri_t uri_capture2    = { .uri = "/api/v1/snapshot",     .method = HTTP_GET,  .handler = capture_handler,            .user_ctx = NULL };
    static const httpd_uri_t uri_cam_pwr_get = { .uri = "/api/v1/camera/power", .method = HTTP_GET,  .handler = camera_power_handler,       .user_ctx = NULL };
    static const httpd_uri_t uri_cam_pwr_pst = { .uri = "/api/v1/camera/power", .method = HTTP_POST, .handler = camera_power_handler,       .user_ctx = NULL };
#else
    static const httpd_uri_t uri_img_post    = { .uri = "/api/v1/image",        .method = HTTP_POST, .handler = image_post_handler,         .user_ctx = NULL };
    static const httpd_uri_t uri_img_get     = { .uri = "/api/v1/image",        .method = HTTP_GET,  .handler = image_get_handler,          .user_ctx = NULL };
#endif

    if (httpd_start(&s_httpd, &config) == ESP_OK) {
      httpd_register_uri_handler(s_httpd, &uri_ping);
      httpd_register_uri_handler(s_httpd, &uri_reboot);
      httpd_register_uri_handler(s_httpd, &uri_status);
      httpd_register_uri_handler(s_httpd, &uri_status2);
      httpd_register_uri_handler(s_httpd, &uri_events);
      httpd_register_uri_handler(s_httpd, &uri_events2);
      httpd_register_uri_handler(s_httpd, &uri_control);
      httpd_register_uri_handler(s_httpd, &uri_post_event);
      httpd_register_uri_handler(s_httpd, &uri_post_event2);

#if HAS_CAMERA_FEATURE
      httpd_register_uri_handler(s_httpd, &uri_root);
      httpd_register_uri_handler(s_httpd, &uri_logs);
      httpd_register_uri_handler(s_httpd, &uri_logs2);
      httpd_register_uri_handler(s_httpd, &uri_capture);
      httpd_register_uri_handler(s_httpd, &uri_capture2);
      httpd_register_uri_handler(s_httpd, &uri_cam_pwr_get);
      httpd_register_uri_handler(s_httpd, &uri_cam_pwr_pst);
#else
      httpd_register_uri_handler(s_httpd, &uri_img_post);
      httpd_register_uri_handler(s_httpd, &uri_img_get);
#endif
      logInfo("HTTP", "Web API server started on port %d", CFG_HTTP_PORT);
    }
  }

  // ArduinoOTA Setup
  ArduinoOTA.setHostname(CFG_MDNS_HOSTNAME);
  ArduinoOTA.onStart([]() {
    setFlashBrightness(0);
#if HAS_CAMERA_FEATURE
    setStreamingPaused(true);
    setCoolingPausedForOTA(true);
#endif
    logWarn("OTA", "Firmware update in progress. Pausing streams and cooling...");
    postEvent(EVENT_OTA_START, "OTA");
  });
  ArduinoOTA.onEnd([]() {
    setFlashBrightness(0);
#if HAS_CAMERA_FEATURE
    setCoolingPausedForOTA(false);
#endif
    logInfo("OTA", "Firmware update finished successfully.");
    postEvent(EVENT_OTA_END, "OTA");
  });
  ArduinoOTA.onError([](ota_error_t err) {
    setFlashBrightness(0);
#if HAS_CAMERA_FEATURE
    setStreamingPaused(false);
    setCoolingPausedForOTA(false);
#endif
    logError("OTA", "Firmware update error (%u)!", (unsigned int)err);
  });
  ArduinoOTA.begin();
  logInfo("OTA", "Wireless OTA Flashing listener ready (Port 3232)");
}

void updateWebApi() {
  ArduinoOTA.handle();
}
