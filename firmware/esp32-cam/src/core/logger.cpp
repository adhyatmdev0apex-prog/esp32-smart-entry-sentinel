#include "core/logger.h"
#include "app_config.h"
#include <stdarg.h>
#include <stdio.h>
#if BOARD_HAS_CAMERA
#include <FS.h>
#include <SPIFFS.h>
static bool s_spiffs_log_ready = false;
static SemaphoreHandle_t s_log_mutex = nullptr;
static const char* LOG_FILE_PATH = "/logs.txt";
static const size_t LOG_MAX_BYTES = 65536; // 64 KB rotation limit (fits 192 KB SPIFFS)
#endif

void initLogger(unsigned long baud_rate) {
  if (!Serial) {
    Serial.begin(baud_rate);
    delay(100);
  }
#if BOARD_HAS_CAMERA
  if (!s_log_mutex) {
    s_log_mutex = xSemaphoreCreateMutex();
  }
  if (!s_spiffs_log_ready) {
    s_spiffs_log_ready = SPIFFS.begin(true);
  }
#endif
}

static void appendLogToSpiffs(const char* line) {
#if BOARD_HAS_CAMERA
  if (!s_spiffs_log_ready || !s_log_mutex) return;
  if (xSemaphoreTake(s_log_mutex, pdMS_TO_TICKS(20)) != pdTRUE) return;
  File f = SPIFFS.open(LOG_FILE_PATH, FILE_APPEND);
  if (f) {
    if (f.size() >= LOG_MAX_BYTES) {
      f.close();
      SPIFFS.remove("/logs.prev.txt");
      SPIFFS.rename(LOG_FILE_PATH, "/logs.prev.txt");
      f = SPIFFS.open(LOG_FILE_PATH, FILE_WRITE);
    }
    if (f) {
      f.print(line);
      f.close();
    }
  }
  xSemaphoreGive(s_log_mutex);
#else
  (void)line;
#endif
}

size_t readSpiffsLogs(char* out, size_t max_len) {
#if BOARD_HAS_CAMERA
  if (!out || max_len == 0 || !s_spiffs_log_ready || !s_log_mutex) return 0;
  if (xSemaphoreTake(s_log_mutex, pdMS_TO_TICKS(100)) != pdTRUE) return 0;
  size_t read_len = 0;
  File f = SPIFFS.open(LOG_FILE_PATH, FILE_READ);
  if (f) {
    size_t sz = f.size();
    size_t start = (sz > max_len - 1) ? (sz - (max_len - 1)) : 0;
    if (start > 0) f.seek(start);
    read_len = f.readBytes(out, max_len - 1);
    out[read_len] = '\0';
    f.close();
  }
  xSemaphoreGive(s_log_mutex);
  return read_len;
#else
  (void)out;
  (void)max_len;
  return 0;
#endif
}

static void logMessage(const char* level, const char* tag, bool persist, const char* fmt, va_list args) {
  char msg[256];
  vsnprintf(msg, sizeof(msg), fmt, args);
  uint32_t sec = millis() / 1000;
  uint32_t ms = millis() % 1000;
  char line[300];
  snprintf(line, sizeof(line), "[%04u.%03u][%s][%s] %s\n", (unsigned int)sec, (unsigned int)ms, level, tag ? tag : "SYS", msg);
  Serial.print(line);
  if (persist) {
    appendLogToSpiffs(line);
  }
}

void logInfo(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("INFO", tag, true, fmt, args);
  va_end(args);
}

void logWarn(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("WARN", tag, true, fmt, args);
  va_end(args);
}

void logError(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("ERROR", tag, true, fmt, args);
  va_end(args);
}

void logDebug(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("DEBUG", tag, false, fmt, args);
  va_end(args);
}
