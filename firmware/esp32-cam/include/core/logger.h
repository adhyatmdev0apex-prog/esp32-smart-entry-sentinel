#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * STRUCTURED LOGGER
 * Consistent logging with component tags and timestamps.
 * ============================================================================
 */

void initLogger(unsigned long baud_rate = 115200);

void logInfo(const char* tag, const char* fmt, ...);
void logWarn(const char* tag, const char* fmt, ...);
void logError(const char* tag, const char* fmt, ...);
void logDebug(const char* tag, const char* fmt, ...);
