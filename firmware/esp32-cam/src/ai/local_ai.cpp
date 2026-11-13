#include "ai/local_ai.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"
#include <WiFiClient.h>
#include <HTTPClient.h>

#if HAS_LOCAL_AI
static uint32_t s_ai_requests = 0;
static uint32_t s_ai_successes = 0;

void initLocalAiClient() {
  logInfo("AI", "Local AI client initialized (Server target: http://%s:%d%s)",
          CFG_LOCAL_AI_HOST, CFG_LOCAL_AI_PORT, CFG_LOCAL_AI_ENDPOINT);
}

AiAnalysisResult sendSnapshotToAiServer(const uint8_t* jpeg_data, size_t length, const char* event_name) {
  AiAnalysisResult res = {};
  if (!jpeg_data || length == 0) return res;

  s_ai_requests++;
  postEvent(EVENT_AI_REQUEST, "AI", (int32_t)length, event_name);

  WiFiClient client;
  HTTPClient http;
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d%s", CFG_LOCAL_AI_HOST, CFG_LOCAL_AI_PORT, CFG_LOCAL_AI_ENDPOINT);

  http.setTimeout(CFG_LOCAL_AI_TIMEOUT_MS);
  if (!http.begin(client, url)) {
    logError("AI", "Failed to connect to AI server: %s", url);
    postEvent(EVENT_AI_ERROR, "AI");
    return res;
  }

  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-ID", CFG_DEVICE_ID);
  http.addHeader("X-Event-Type", event_name ? event_name : "UNKNOWN");

  int httpCode = http.POST(const_cast<uint8_t*>(jpeg_data), length);
  if (httpCode == HTTP_CODE_OK) {
    String payload = http.getString();
    logInfo("AI", "Server response: %s", payload.c_str());
    res.success = true;
    s_ai_successes++;
    postEvent(EVENT_AI_RESULT, "AI", httpCode);
  } else {
    logWarn("AI", "HTTP POST failed: %d (%s)", httpCode, http.errorToString(httpCode).c_str());
    postEvent(EVENT_AI_ERROR, "AI", httpCode);
  }

  http.end();
  return res;
}

void getLocalAiDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"ai\":{\"enabled\":true,\"requests\":%u,\"success\":%u}",
           (unsigned int)s_ai_requests, (unsigned int)s_ai_successes);
}

#else

void initLocalAiClient() {}
AiAnalysisResult sendSnapshotToAiServer(const uint8_t*, size_t, const char*) {
  return AiAnalysisResult{false, false, 0, 0, "", 0.0f};
}
void getLocalAiDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len, "\"ai\":{\"enabled\":false}");
}

#endif
