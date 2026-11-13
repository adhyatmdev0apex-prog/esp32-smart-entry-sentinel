#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * SYSTEM HEALTH & HARDWARE DIAGNOSTICS
 * Tracks CPU, Heap, PSRAM, Junction Temperature, Uptime, and Brownout Safety.
 * ============================================================================
 */

struct SystemStats {
  uint32_t cpu_mhz;
  uint32_t uptime_s;
  uint32_t free_heap;
  uint32_t min_free_heap;
  bool     has_psram;
  uint32_t total_psram;
  uint32_t free_psram;
  float    temp_c;
};

void initSystem();
void updateSystem();

SystemStats getSystemStats();
void getSystemDiagnosticsJson(char* buf, size_t max_len);
void requestReboot(uint32_t delay_ms = 500);
