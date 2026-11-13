#include "core/logger.h"
#include <stdarg.h>
#include <stdio.h>

void initLogger(unsigned long baud_rate) {
  if (!Serial) {
    Serial.begin(baud_rate);
    delay(100);
  }
}

static void logMessage(const char* level, const char* tag, const char* fmt, va_list args) {
  char msg[256];
  vsnprintf(msg, sizeof(msg), fmt, args);
  uint32_t sec = millis() / 1000;
  uint32_t ms = millis() % 1000;
  Serial.printf("[%04u.%03u][%s][%s] %s\n", (unsigned int)sec, (unsigned int)ms, level, tag ? tag : "SYS", msg);
}

void logInfo(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("INFO", tag, fmt, args);
  va_end(args);
}

void logWarn(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("WARN", tag, fmt, args);
  va_end(args);
}

void logError(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("ERROR", tag, fmt, args);
  va_end(args);
}

void logDebug(const char* tag, const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  logMessage("DEBUG", tag, fmt, args);
  va_end(args);
}
