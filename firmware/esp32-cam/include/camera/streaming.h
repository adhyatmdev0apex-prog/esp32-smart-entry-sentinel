#pragma once
#include <Arduino.h>
#include "app_config.h"

#if HAS_CAMERA_FEATURE

void initStreamingServer();
void setStreamingPaused(bool paused);
void getStreamingDiagnosticsJson(char* buf, size_t max_len);

// Stage a snapshot frame from the stream task so /capture completes in <1ms without locking camera mutex
bool copyLatestStreamFrame(uint8_t* out_buf, size_t max_len, size_t* out_len);

#else

inline void initStreamingServer() {}
inline void setStreamingPaused(bool) {}
inline void getStreamingDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"stream\":{\"enabled\":false}");
}
inline bool copyLatestStreamFrame(uint8_t*, size_t, size_t*) { return false; }

#endif
