#include <Arduino.h>
#include <esp_camera.h>
#include <esp_http_server.h>

#include "camera.h"
#include "stream_server.h"

static httpd_handle_t stream_httpd = NULL;
static httpd_handle_t camera_httpd = NULL;
static SemaphoreHandle_t s_camera_mutex = NULL;

#define PART_BOUNDARY "123456789000000000000987654321"
static const char* _STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* _STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* _STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static esp_err_t stream_handler(httpd_req_t *req) {
  if (s_camera_mutex == NULL) {
    Serial.println("[STREAM] Error: camera mutex not initialized");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  
  camera_fb_t * fb = NULL;
  esp_err_t res = ESP_OK;
  size_t _jpg_buf_len = 0;
  uint8_t * _jpg_buf = NULL;
  char part_buf[64];
  int consecutive_failures = 0;
  const int MAX_CONSECUTIVE_FAILURES = 10;

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) {
    return res;
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  while (true) {
    if (xSemaphoreTake(s_camera_mutex, pdMS_TO_TICKS(500)) != pdTRUE) {
      consecutive_failures++;
      Serial.printf("[STREAM] Mutex take failed (%d/%d)\n", consecutive_failures, MAX_CONSECUTIVE_FAILURES);
      if (consecutive_failures >= MAX_CONSECUTIVE_FAILURES) {
        Serial.println("[STREAM] Aborting stream: mutex acquisition limit reached");
        res = ESP_FAIL;
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    fb = esp_camera_fb_get();
    if (!fb) {
      xSemaphoreGive(s_camera_mutex);
      consecutive_failures++;
      Serial.printf("[STREAM] esp_camera_fb_get returned NULL (%d/%d)\n", consecutive_failures, MAX_CONSECUTIVE_FAILURES);
      if (consecutive_failures >= MAX_CONSECUTIVE_FAILURES) {
        Serial.println("[STREAM] Aborting stream: null frame limit reached");
        res = ESP_FAIL;
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    if (fb->format != PIXFORMAT_JPEG) {
      bool jpeg_converted = frame2jpg(fb, 80, &_jpg_buf, &_jpg_buf_len);
      esp_camera_fb_return(fb);
      fb = NULL;
      xSemaphoreGive(s_camera_mutex);

      if (!jpeg_converted) {
        consecutive_failures++;
        Serial.printf("[STREAM] JPEG conversion failed (%d/%d)\n", consecutive_failures, MAX_CONSECUTIVE_FAILURES);
        if (consecutive_failures >= MAX_CONSECUTIVE_FAILURES) {
          res = ESP_FAIL;
          break;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
    } else {
      _jpg_buf_len = fb->len;
      _jpg_buf = fb->buf;
    }

    // Valid frame acquired
    consecutive_failures = 0;

    // 1. Send _STREAM_BOUNDARY before each part
    res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));

    // 2. Send part headers
    if (res == ESP_OK) {
      int hlen = snprintf(part_buf, sizeof(part_buf), _STREAM_PART, (unsigned int)_jpg_buf_len);
      if (hlen < 0 || (size_t)hlen >= sizeof(part_buf)) {
        res = ESP_FAIL;
      } else {
        res = httpd_resp_send_chunk(req, part_buf, (size_t)hlen);
      }
    }

    // 3. Send JPEG bytes
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, reinterpret_cast<const char*>(_jpg_buf), _jpg_buf_len);
    }

    // Release framebuffer and mutex after sending finishes or fails
    if (fb != NULL) {
      esp_camera_fb_return(fb);
      fb = NULL;
      _jpg_buf = NULL;
      xSemaphoreGive(s_camera_mutex);
    } else if (_jpg_buf != NULL) {
      free(_jpg_buf);
      _jpg_buf = NULL;
    }

    if (res != ESP_OK) {
      break;
    }
  }

  return res;
}

static esp_err_t ping_handler(httpd_req_t* req) {
  httpd_resp_set_type(req, "text/plain");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "ESP32-CAM OK", 12);
}

static esp_err_t reboot_handler(httpd_req_t* req) {
  httpd_resp_send(req, "Rebooting...", 12);
  vTaskDelay(pdMS_TO_TICKS(500));
  esp_restart();
  return ESP_OK;
}

static esp_err_t status_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char diag[384];
  getCameraDiagnostics(diag, sizeof(diag));
  char buf[512];
  snprintf(buf, sizeof(buf), "{\"status\":\"ok\",\"cpu_mhz\":%u,\"psram\":%s,\"free_heap\":%u,%s}",
           (unsigned int)getCpuFrequencyMhz(),
           psramFound() ? "true" : "false",
           (unsigned int)ESP.getFreeHeap(),
           diag);
  return httpd_resp_send(req, buf, strlen(buf));
}

static esp_err_t capture_handler(httpd_req_t* req) {
  if (s_camera_mutex == NULL) {
    Serial.println("[CAPTURE] Error: camera mutex not initialized");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  if (xSemaphoreTake(s_camera_mutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    Serial.println("[CAPTURE] Error: failed to acquire camera mutex");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    recordCaptureResult(false, 0);
    xSemaphoreGive(s_camera_mutex);
    Serial.println("[CAPTURE] Error: esp_camera_fb_get returned NULL");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  recordCaptureResult(true, fb->len);

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, reinterpret_cast<const char*>(fb->buf), fb->len);

  esp_camera_fb_return(fb);
  xSemaphoreGive(s_camera_mutex);
  return res;
}

void startCameraServer() {
  if (s_camera_mutex == NULL) {
    s_camera_mutex = xSemaphoreCreateMutex();
  }
  if (s_camera_mutex == NULL) {
    Serial.println("[HTTP] Error: failed to create camera mutex, aborting server startup");
    return;
  }

  // 1. Port 80 for Ping, Status, Capture, Reboot
  httpd_config_t config_ctl = HTTPD_DEFAULT_CONFIG();
  config_ctl.server_port = 80;

  static const httpd_uri_t ping_uri = { .uri = "/ping", .method = HTTP_GET, .handler = ping_handler, .user_ctx = NULL };
  static const httpd_uri_t reboot_uri = { .uri = "/reboot", .method = HTTP_GET, .handler = reboot_handler, .user_ctx = NULL };
  static const httpd_uri_t status_uri = { .uri = "/status", .method = HTTP_GET, .handler = status_handler, .user_ctx = NULL };
  static const httpd_uri_t capture_uri = { .uri = "/capture", .method = HTTP_GET, .handler = capture_handler, .user_ctx = NULL };

  if (httpd_start(&camera_httpd, &config_ctl) == ESP_OK) {
    httpd_register_uri_handler(camera_httpd, &ping_uri);
    httpd_register_uri_handler(camera_httpd, &reboot_uri);
    httpd_register_uri_handler(camera_httpd, &status_uri);
    httpd_register_uri_handler(camera_httpd, &capture_uri);
    Serial.println("Port 80 server started");
  }

  // 2. Port 81 for dedicated MJPEG Stream
  httpd_config_t config_stream = HTTPD_DEFAULT_CONFIG();
  config_stream.server_port = 81;
  config_stream.ctrl_port = 32769;

  static const httpd_uri_t stream_uri = {
    .uri       = "/stream",
    .method    = HTTP_GET,
    .handler   = stream_handler,
    .user_ctx  = NULL
  };

  if (httpd_start(&stream_httpd, &config_stream) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
    Serial.println("Port 81 stream server started");
  }
}
