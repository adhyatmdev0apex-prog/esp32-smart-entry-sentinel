#include "camera/streaming.h"
#include "camera/camera_manager.h"
#include "core/logger.h"
#include "app_config.h"
#include <WiFi.h>

#if HAS_CAMERA_FEATURE
#include <esp_camera.h>
#include <lwip/sockets.h>
#include <lwip/netdb.h>
#include <errno.h>

static const size_t STAGE_BUF_CAPACITY = 262144; // 256 KB
static uint8_t* s_stage_buf = NULL;
static size_t s_stage_len = 0;
static uint32_t s_stage_timestamp_ms = 0;
static SemaphoreHandle_t s_stage_mutex = NULL;

static volatile bool s_paused = false;
static volatile uint32_t s_frames_sent = 0;
static volatile uint32_t s_drops = 0;
static volatile int s_active_clients = 0;
static volatile int s_last_errno = 0;
static const char* volatile s_stream_stage = "init";

static TaskHandle_t s_stream_task_handle = NULL;
static const size_t DRAM_CHUNK_SIZE = 1460; // 1 x TCP MSS

#define PART_BOUNDARY "123456789000000000000987654321"
static const char* STREAM_HTTP_HEADER =
  "HTTP/1.1 200 OK\r\n"
  "Content-Type: multipart/x-mixed-replace;boundary=" PART_BOUNDARY "\r\n"
  "Access-Control-Allow-Origin: *\r\n"
  "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"
  "Pragma: no-cache\r\n"
  "Connection: close\r\n\r\n";

static const char* STREAM_PART_FMT =
  "\r\n--" PART_BOUNDARY "\r\n"
  "Content-Type: image/jpeg\r\n"
  "Content-Length: %u\r\n\r\n";

static bool send_all_chunked(int sock, const uint8_t* psram_data, size_t total_len) {
  uint8_t chunk[DRAM_CHUNK_SIZE];
  size_t offset = 0;
  while (offset < total_len) {
    size_t chunk_len = total_len - offset;
    if (chunk_len > DRAM_CHUNK_SIZE) chunk_len = DRAM_CHUNK_SIZE;
    memcpy(chunk, psram_data + offset, chunk_len);

    size_t chunk_sent = 0;
    while (chunk_sent < chunk_len) {
      ssize_t res = send(sock, (const char*)(chunk + chunk_sent), chunk_len - chunk_sent, 0);
      if (res <= 0) {
        if (res < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
          vTaskDelay(pdMS_TO_TICKS(1));
          continue;
        }
        s_last_errno = errno;
        return false;
      }
      chunk_sent += res;
    }
    offset += chunk_len;
    vTaskDelay(pdMS_TO_TICKS(1));
  }
  return true;
}

static void mjpegStreamTask(void* pvParameters) {
  // CRITICAL: Sockets cannot be created before lwIP tcpip mbox is initialized
  while (WiFi.status() != WL_CONNECTED) {
    vTaskDelay(pdMS_TO_TICKS(100));
  }

  int server_sock = -1;
  while (server_sock < 0) {
    server_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (server_sock < 0) {
      logWarn("STREAM", "socket() failed (errno %d), retrying...", errno);
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }

  int opt = 1;
  setsockopt(server_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

  struct sockaddr_in serv_addr;
  memset(&serv_addr, 0, sizeof(serv_addr));
  serv_addr.sin_family = AF_INET;
  serv_addr.sin_addr.s_addr = htonl(INADDR_ANY);
  serv_addr.sin_port = htons(CFG_STREAM_PORT);

  if (bind(server_sock, (struct sockaddr*)&serv_addr, sizeof(serv_addr)) < 0) {
    close(server_sock);
    vTaskDelete(NULL);
    return;
  }

  listen(server_sock, 2);
  logInfo("STREAM", "MJPEG Server listening on port %d", CFG_STREAM_PORT);

  while (true) {
    if (s_paused || !isCameraSensorActive() || !isCameraReady()) {
      s_stream_stage = "standby";
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }

    s_stream_stage = "accept";
    struct sockaddr_in client_addr;
    socklen_t client_addr_len = sizeof(client_addr);
    int client_sock = accept(server_sock, (struct sockaddr*)&client_addr, &client_addr_len);
    if (client_sock < 0) {
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }

    struct timeval tv;
    tv.tv_sec = 4;
    tv.tv_usec = 0;
    setsockopt(client_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(client_sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    int nodelay = 1;
    setsockopt(client_sock, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    s_active_clients++;
    s_stream_stage = "hdr";
    send(client_sock, STREAM_HTTP_HEADER, strlen(STREAM_HTTP_HEADER), 0);

    char part_hdr[80];
    const uint32_t target_interval_ms = 40; // ~25 FPS max
    int consecutive_null_fb = 0;

    while (true) {
      if (s_paused || !isCameraSensorActive() || !isCameraReady()) break;

      uint32_t frame_start = millis();

      s_stream_stage = "mutex";
      if (!takeCameraMutex(1500)) {
        vTaskDelay(pdMS_TO_TICKS(5));
        continue;
      }

      s_stream_stage = "fb_get";
      camera_fb_t* fb = esp_camera_fb_get();
      if (!fb || fb->len == 0 || fb->len > STAGE_BUF_CAPACITY) {
        if (fb) esp_camera_fb_return(fb);
        giveCameraMutex();
        if (++consecutive_null_fb >= 5) {
          logWarn("STREAM", "DVP DMA stall (5 NULL frames) -> power-cycling sensor");
          setCameraSensorActive(false);
          setCameraSensorActive(true);
          break;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
        continue;
      }
      consecutive_null_fb = 0;

      s_stream_stage = "stage";
      // Stage copy in PSRAM
      if (s_stage_mutex && xSemaphoreTake(s_stage_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        memcpy(s_stage_buf, fb->buf, fb->len);
        s_stage_len = fb->len;
        s_stage_timestamp_ms = millis();
        xSemaphoreGive(s_stage_mutex);
      }

      s_stream_stage = "send";
      int hdr_len = snprintf(part_hdr, sizeof(part_hdr), STREAM_PART_FMT, fb->len);
      bool ok = (send(client_sock, part_hdr, hdr_len, 0) == hdr_len);
      if (ok) {
        ok = send_all_chunked(client_sock, fb->buf, fb->len);
      }

      s_stream_stage = "fb_ret";
      esp_camera_fb_return(fb);
      giveCameraMutex();

      if (!ok) {
        s_drops++;
        break;
      }

      s_frames_sent++;
      uint32_t elapsed = millis() - frame_start;
      if (elapsed < target_interval_ms) {
        vTaskDelay(pdMS_TO_TICKS(target_interval_ms - elapsed));
      } else {
        vTaskDelay(pdMS_TO_TICKS(2));
      }
    }

    close(client_sock);
    s_active_clients--;
  }
}

void initStreamingServer() {
  if (s_stage_mutex == NULL) {
    s_stage_mutex = xSemaphoreCreateMutex();
  }
  if (s_stage_buf == NULL) {
    s_stage_buf = (uint8_t*)heap_caps_malloc(STAGE_BUF_CAPACITY, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_stage_buf) {
      s_stage_buf = (uint8_t*)malloc(65536);
    }
  }

  if (s_stream_task_handle == NULL) {
    xTaskCreatePinnedToCore(
      mjpegStreamTask,
      "mjpeg_stream",
      8192,
      NULL,
      5,
      &s_stream_task_handle,
      1 // Core 1 (APP_CPU)
    );
  }
}

void setStreamingPaused(bool paused) {
  s_paused = paused;
}

bool copyLatestStreamFrame(uint8_t* out_buf, size_t max_len, size_t* out_len) {
  if (!out_buf || !out_len || !s_stage_mutex || !s_stage_buf) return false;
  if (xSemaphoreTake(s_stage_mutex, pdMS_TO_TICKS(50)) != pdTRUE) return false;

  bool ok = false;
  if (s_stage_len > 0 && s_stage_len <= max_len && (millis() - s_stage_timestamp_ms < 500)) {
    memcpy(out_buf, s_stage_buf, s_stage_len);
    *out_len = s_stage_len;
    ok = true;
  }
  xSemaphoreGive(s_stage_mutex);
  return ok;
}

void getStreamingDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len,
           "\"stream\":{\"active_clients\":%d,\"sent\":%u,\"drops\":%u,\"last_err\":%d,\"stage\":\"%s\"}",
           s_active_clients,
           (unsigned int)s_frames_sent,
           (unsigned int)s_drops,
           s_last_errno,
           s_stream_stage);
}

#endif
