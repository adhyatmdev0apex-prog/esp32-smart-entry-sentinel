#include "camera/camera_manager.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"

#if HAS_CAMERA_FEATURE
#include <esp_log.h>
#include <stdarg.h>

static bool s_cam_ready = false;
static volatile bool s_cam_sensor_active = false;
static int s_cam_pid = -1;
static int s_cam_framesize = -1;
static int s_cam_pixformat = -1;
static int s_xclk_mhz = CFG_CAMERA_XCLK_MHZ;
static uint32_t s_cap_ok = 0;
static uint32_t s_cap_fail = 0;
static size_t s_last_len = 0;
static char s_cam_log[160] = "none";
static SemaphoreHandle_t s_camera_mutex = NULL;
static uint32_t s_last_retry_ms = 0;

static int cam_log_vprintf(const char* fmt, va_list args) {
  char tmp[160];
  va_list copy;
  va_copy(copy, args);
  int n = vsnprintf(tmp, sizeof(tmp), fmt, copy);
  va_end(copy);
  if (n > 0) {
    size_t j = 0;
    for (int i = 0; i < n && tmp[i] != '\0' && j < sizeof(s_cam_log) - 1; i++) {
      char c = tmp[i];
      if (c == '\r' || c == '\n') c = ' ';
      else if (c == '"' || c == '\\') c = '\'';
      if (c >= 32 && c < 127) s_cam_log[j++] = c;
    }
    s_cam_log[j] = '\0';
  }
  return vprintf(fmt, args);
}

bool initCamera(bool fast_wake) {
  if (s_camera_mutex == NULL) {
    s_camera_mutex = xSemaphoreCreateMutex();
  }
  s_cam_sensor_active = true;

  esp_log_level_set("*", ESP_LOG_INFO);
  esp_log_set_vprintf(cam_log_vprintf);

  // Power-cycle the sensor via PWDN (fast 15ms wake from clean standby, full cycle on cold/recovery)
  if (PIN_CAM_PWDN >= 0) {
    pinMode(PIN_CAM_PWDN, OUTPUT);
    if (fast_wake) {
      digitalWrite(PIN_CAM_PWDN, LOW);
      delay(15);
    } else {
      digitalWrite(PIN_CAM_PWDN, HIGH);
      delay(250);
      digitalWrite(PIN_CAM_PWDN, LOW);
      delay(100);
    }
  }

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = PIN_CAM_D0;
  config.pin_d1       = PIN_CAM_D1;
  config.pin_d2       = PIN_CAM_D2;
  config.pin_d3       = PIN_CAM_D3;
  config.pin_d4       = PIN_CAM_D4;
  config.pin_d5       = PIN_CAM_D5;
  config.pin_d6       = PIN_CAM_D6;
  config.pin_d7       = PIN_CAM_D7;
  config.pin_xclk     = PIN_CAM_XCLK;
  config.pin_pclk     = PIN_CAM_PCLK;
  config.pin_vsync    = PIN_CAM_VSYNC;
  config.pin_href     = PIN_CAM_HREF;
  config.pin_sccb_sda = PIN_CAM_SIOD;
  config.pin_sccb_scl = PIN_CAM_SIOC;
  config.pin_pwdn     = PIN_CAM_PWDN;
  config.pin_reset    = PIN_CAM_RESET;

  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.sccb_i2c_port = 0;

  if (psramFound()) {
    config.frame_size   = (framesize_t)CFG_CAMERA_FRAME_SIZE;
    config.jpeg_quality = CFG_CAMERA_JPEG_QUALITY;
    config.fb_count     = 2;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 14;
    config.fb_count     = 1;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location  = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    esp_camera_deinit();
    delay(100);
    // Probe fallback at 20MHz
    if (config.xclk_freq_hz != 20000000) {
      logWarn("CAM", "Probing fallback at 20 MHz XCLK...");
      config.xclk_freq_hz = 20000000;
      s_xclk_mhz = 20;
      err = esp_camera_init(&config);
    }
  }

  if (err != ESP_OK) {
    esp_camera_deinit();
    logError("CAM", "Camera initialization failed (0x%x)", err);
    s_cam_ready = false;
    postEvent(EVENT_CAMERA_ERROR, "CAM", (int32_t)err);
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s != NULL) {
    s_cam_pid = s->id.PID;
    s_cam_framesize = (int)s->status.framesize;
    s_cam_pixformat = (int)s->pixformat;
    s->set_hmirror(s, CFG_CAMERA_HFLIP);
    s->set_vflip(s, CFG_CAMERA_VFLIP);
    logInfo("CAM", "OV2640 Sensor online! PID: 0x%02x, Framesize: %d, XCLK: %d MHz",
            s_cam_pid, s_cam_framesize, s_xclk_mhz);
  }

  s_cam_ready = true;
  postEvent(EVENT_CAMERA_STARTED, "CAM", s_cam_pid);
  return true;
}

void initCameraStandby() {
  if (s_camera_mutex == NULL) {
    s_camera_mutex = xSemaphoreCreateMutex();
  }
  s_cam_sensor_active = false;
  s_cam_ready = false;
  if (PIN_CAM_PWDN >= 0) {
    pinMode(PIN_CAM_PWDN, OUTPUT);
    digitalWrite(PIN_CAM_PWDN, HIGH);
  }
  logInfo("CAM", "Camera sensor in low-power STANDBY (PWDN=HIGH)");
}

void updateCamera() {
  if (s_cam_sensor_active && !s_cam_ready) {
    uint32_t now = millis();
    if (now - s_last_retry_ms >= 4000) {
      s_last_retry_ms = now;
      logWarn("CAM", "Retrying camera sensor recovery...");
      initCamera(false);
    }
  }
}

bool isCameraReady() {
  return s_cam_ready;
}

bool isCameraSensorActive() {
  return s_cam_sensor_active;
}

bool setCameraSensorActive(bool active) {
  if (active) {
    if (s_cam_sensor_active && s_cam_ready) return true;
    uint32_t t0 = millis();
    bool ok = initCamera(true);
    if (!ok) {
      ok = initCamera(false);
    }
    logInfo("CAM", "Camera sensor WAKE %s in %u ms", ok ? "OK" : "FAILED", (unsigned)(millis() - t0));
    return ok;
  } else {
    if (!s_cam_sensor_active && !s_cam_ready) return true;
    s_cam_sensor_active = false;
    s_cam_ready = false;
    takeCameraMutex(600);
    esp_camera_deinit();
    if (PIN_CAM_PWDN >= 0) {
      pinMode(PIN_CAM_PWDN, OUTPUT);
      digitalWrite(PIN_CAM_PWDN, HIGH);
    }
    giveCameraMutex();
    logInfo("CAM", "Camera sensor entered low-power STANDBY");
    return true;
  }
}

bool takeCameraMutex(uint32_t timeout_ms) {
  if (!s_camera_mutex) return true;
  return (xSemaphoreTake(s_camera_mutex, pdMS_TO_TICKS(timeout_ms)) == pdTRUE);
}

void giveCameraMutex() {
  if (s_camera_mutex) {
    xSemaphoreGive(s_camera_mutex);
  }
}

camera_fb_t* captureSnapshotFb() {
  if (!s_cam_ready) return NULL;
  if (!takeCameraMutex(1500)) return NULL;
  camera_fb_t* fb = esp_camera_fb_get();
  giveCameraMutex();
  if (fb && fb->len > 0) {
    recordCaptureMetrics(true, fb->len);
    postEvent(EVENT_SNAPSHOT_CAPTURED, "CAM", (int32_t)fb->len);
  } else {
    recordCaptureMetrics(false, 0);
  }
  return fb;
}

void returnSnapshotFb(camera_fb_t* fb) {
  if (fb) {
    esp_camera_fb_return(fb);
  }
}

void autoTuneCameraXclk(int wifi_channel) {
  if (wifi_channel < 1 || wifi_channel > 13) return;
  int center_mhz = 2407 + (5 * wifi_channel);
  const int candidates[] = { 24, 18, 22, 15, 12 };
  int best_xclk = 24;
  int best_dist = -1;

  for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++) {
    int x = candidates[i];
    int rem = center_mhz % x;
    int dist = (rem < (x - rem)) ? rem : (x - rem);
    if (dist > best_dist) {
      best_dist = dist;
      best_xclk = x;
    }
  }

  if (best_xclk != s_xclk_mhz) {
    sensor_t* s = esp_camera_sensor_get();
    if (s != NULL && s->set_xclk != NULL) {
      if (s->set_xclk(s, LEDC_TIMER_0, best_xclk) == 0) {
        s_xclk_mhz = best_xclk;
        logInfo("CAM", "Auto-tuned XCLK=%d MHz for Wi-Fi Ch %d (guard=%d MHz)", best_xclk, wifi_channel, best_dist);
      }
    }
  }
}

void recordCaptureMetrics(bool ok, size_t len) {
  if (ok) {
    s_cap_ok++;
    s_last_len = len;
  } else {
    s_cap_fail++;
  }
}

void getCameraDiagnosticsJson(char* buf, size_t max_len) {
  sensor_t* s = esp_camera_sensor_get();
  snprintf(buf, max_len,
           "\"camera\":{\"ready\":%s,\"sensor_active\":%s,\"xclk_mhz\":%d,\"cam_pid\":%d,\"framesize\":%d,"
           "\"cap_ok\":%u,\"cap_fail\":%u,\"last_len\":%u,\"cam_log\":\"%s\"}",
           s_cam_ready ? "true" : "false",
           s_cam_sensor_active ? "true" : "false",
           s_xclk_mhz,
           s_cam_pid,
           s ? (int)s->status.framesize : -1,
           (unsigned int)s_cap_ok,
           (unsigned int)s_cap_fail,
           (unsigned int)s_last_len,
           s_cam_log);
}

#endif
