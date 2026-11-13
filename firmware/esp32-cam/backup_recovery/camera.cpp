#include <Arduino.h>
#include <esp_camera.h>
#include <esp_log.h>
#include <stdarg.h>
#include "soc/gpio_reg.h"

#include "camera.h"
#include "camera_pins.h"

static int s_cam_pid = -1;
static int s_cam_framesize = -1;
static int s_cam_pixformat = -1;
static uint32_t s_cap_ok = 0;
static uint32_t s_cap_fail = 0;
static size_t s_last_len = 0;
static char s_cam_log[160] = "none";

static int cam_log_vprintf(const char* fmt, va_list args) {
  char tmp[160];
  va_list copy;
  va_copy(copy, args);
  int n = vsnprintf(tmp, sizeof(tmp), fmt, copy);
  va_end(copy);
  if (n > 0) {
    // Sanitize quotes and control chars for safe JSON embedding
    size_t j = 0;
    for (int i = 0; i < n && tmp[i] != '\0' && j < sizeof(s_cam_log) - 1; i++) {
      char c = tmp[i];
      if (c == '\r' || c == '\n') c = ' ';
      else if (c == '"' || c == '\\') c = '\'';
      if (c >= 32 && c < 127) {
        s_cam_log[j++] = c;
      }
    }
    s_cam_log[j] = '\0';
  }
  return vprintf(fmt, args);
}

void recordCaptureResult(bool ok, size_t len) {
  if (ok) {
    s_cap_ok++;
    s_last_len = len;
  } else {
    s_cap_fail++;
  }
}

void getCameraDiagnostics(char* buf, size_t len) {
  uint32_t vsync_edges = 0;
  uint32_t href_edges = 0;
  uint32_t pclk_edges = 0;
  uint32_t prev = REG_READ(GPIO_IN_REG);
  for (int i = 0; i < 40000; i++) {
    uint32_t cur = REG_READ(GPIO_IN_REG);
    uint32_t diff = cur ^ prev;
    if (diff & (1UL << VSYNC_GPIO_NUM)) vsync_edges++;
    if (diff & (1UL << HREF_GPIO_NUM)) href_edges++;
    if (diff & (1UL << PCLK_GPIO_NUM)) pclk_edges++;
    prev = cur;
  }
  snprintf(buf, len,
           "\"fw\":\"cam_fix_v1\",\"cam_pid\":%d,\"cam_fs\":%d,\"cam_pf\":%d,"
           "\"cap_ok\":%u,\"cap_fail\":%u,\"last_len\":%u,"
           "\"vsync_edges\":%u,\"href_edges\":%u,\"pclk_edges\":%u,"
           "\"cam_log\":\"%s\"",
           s_cam_pid, s_cam_framesize, s_cam_pixformat,
           (unsigned int)s_cap_ok, (unsigned int)s_cap_fail, (unsigned int)s_last_len,
           (unsigned int)vsync_edges, (unsigned int)href_edges, (unsigned int)pclk_edges,
           s_cam_log);
}

bool initializeCamera() {
  esp_log_level_set("*", ESP_LOG_INFO);
  esp_log_set_vprintf(cam_log_vprintf);

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.sccb_i2c_port = 0;

  if (psramFound()) {
    config.frame_size = FRAMESIZE_SVGA;
    config.jpeg_quality = 10;
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count = 1;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[CAMERA] Init failed with error 0x%x\n", err);
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s != NULL) {
    s_cam_pid = s->id.PID;
    s_cam_framesize = (int)s->status.framesize;
    s_cam_pixformat = (int)s->pixformat;
    Serial.printf("[CAMERA] Sensor PID=0x%02x framesize=%d pixformat=%d\n",
                  s_cam_pid, s_cam_framesize, s_cam_pixformat);
  }
  return true;
}
