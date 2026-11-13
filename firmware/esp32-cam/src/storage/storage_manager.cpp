#include "storage/storage_manager.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"
#include <stdio.h>

#if HAS_SD_STORAGE
#include "FS.h"
#include "SD_MMC.h"

static bool s_sd_mounted = false;

void initStorage() {
  // Use 1-bit SD mode so GPIO 4 and GPIO 13 are not occupied by SD bus
  if (SD_MMC.begin("/sdcard", true)) {
    s_sd_mounted = true;
    uint64_t total_mb = SD_MMC.totalBytes() / (1024 * 1024);
    uint64_t used_mb = SD_MMC.usedBytes() / (1024 * 1024);
    logInfo("SD", "MicroSD mounted! Size: %llu MB (Used: %llu MB)", total_mb, used_mb);
    postEvent(EVENT_SD_MOUNTED, "SD", (int32_t)total_mb);
  } else {
    s_sd_mounted = false;
    logWarn("SD", "MicroSD not detected or mount failed (continuing without SD)");
    postEvent(EVENT_SD_ERROR, "SD");
  }
}

bool isStorageMounted() {
  return s_sd_mounted;
}

bool saveSnapshotToStorage(const uint8_t* jpeg_data, size_t length, const char* label) {
  if (!s_sd_mounted || !jpeg_data || length == 0) return false;

  char filename[64];
  snprintf(filename, sizeof(filename), "/snap_%s_%lu.jpg", label ? label : "img", millis());

  File file = SD_MMC.open(filename, FILE_WRITE);
  if (!file) {
    logError("SD", "Failed to open file for writing: %s", filename);
    return false;
  }
  size_t written = file.write(jpeg_data, length);
  file.close();

  if (written == length) {
    logInfo("SD", "Saved snapshot to SD: %s (%u bytes)", filename, (unsigned int)length);
    return true;
  }
  return false;
}

void getStorageDiagnosticsJson(char* buf, size_t max_len) {
  if (s_sd_mounted) {
    snprintf(buf, max_len, "\"storage\":{\"enabled\":true,\"mounted\":true,\"total_mb\":%llu,\"used_mb\":%llu}",
             SD_MMC.totalBytes() / (1024 * 1024),
             SD_MMC.usedBytes() / (1024 * 1024));
  } else {
    snprintf(buf, max_len, "\"storage\":{\"enabled\":true,\"mounted\":false}");
  }
}

#else

void initStorage() {}
bool isStorageMounted() { return false; }
bool saveSnapshotToStorage(const uint8_t*, size_t, const char*) { return false; }
void getStorageDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"storage\":{\"enabled\":false}");
}

#endif
