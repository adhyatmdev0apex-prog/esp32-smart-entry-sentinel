#include "core/system.h"
#include "core/logger.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <esp_task_wdt.h>
#include <esp_system.h>
#include <stdio.h>

extern "C" uint8_t temprature_sens_read();

static uint32_t s_boot_time_ms = 0;
static bool s_reboot_pending = false;
static uint32_t s_reboot_target_ms = 0;

static const char* getResetReasonName() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "POWERON";
    case ESP_RST_SW:       return "SW_RESET";
    case ESP_RST_PANIC:    return "PANIC";
    case ESP_RST_INT_WDT:  return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT:      return "WDT";
    case ESP_RST_DEEPSLEEP:return "DEEPSLEEP";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    default:               return "OTHER";
  }
}

void initSystem() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); // Disable brownout detector for WiFi/Camera peak transients
  s_boot_time_ms = millis();
  esp_task_wdt_init(30, true);
  esp_task_wdt_add(NULL);
}

void updateSystem() {
  esp_task_wdt_reset();
  if (s_reboot_pending && millis() >= s_reboot_target_ms) {
    logWarn("SYS", "Reboot triggered by request...");
    delay(100);
    esp_restart();
  }
}

SystemStats getSystemStats() {
  SystemStats stats;
  stats.cpu_mhz = getCpuFrequencyMhz();
  stats.uptime_s = (millis() - s_boot_time_ms) / 1000;
  stats.free_heap = ESP.getFreeHeap();
  stats.min_free_heap = ESP.getMinFreeHeap();
  stats.has_psram = psramFound();
  stats.total_psram = stats.has_psram ? ESP.getPsramSize() : 0;
  stats.free_psram = stats.has_psram ? ESP.getFreePsram() : 0;
  stats.temp_c = temperatureRead();
  return stats;
}

void getSystemDiagnosticsJson(char* buf, size_t max_len) {
  SystemStats st = getSystemStats();
  snprintf(buf, max_len,
           "\"cpu_mhz\":%u,\"uptime_s\":%u,\"free_heap\":%u,\"min_free_heap\":%u,"
           "\"psram\":%s,\"psram_size\":%u,\"free_psram\":%u,\"temp_c\":%.1f,\"reset_reason\":\"%s\"",
           (unsigned int)st.cpu_mhz,
           (unsigned int)st.uptime_s,
           (unsigned int)st.free_heap,
           (unsigned int)st.min_free_heap,
           st.has_psram ? "true" : "false",
           (unsigned int)st.total_psram,
           (unsigned int)st.free_psram,
           st.temp_c,
           getResetReasonName());
}

void requestReboot(uint32_t delay_ms) {
  s_reboot_pending = true;
  s_reboot_target_ms = millis() + delay_ms;
}
