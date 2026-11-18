#pragma once
#include <Arduino.h>
#include "app_config.h"

#if HAS_CAMERA_FEATURE
#include <esp_camera.h>

typedef enum {
  COOLING_POLICY_FIXED_MAX,    // HD, SXGA, UXGA: strictly 100% duty cycle
  COOLING_POLICY_XGA,          // XGA: min 75%, highly temp-sensitive, 100% at threshold
  COOLING_POLICY_LOW_POWER,    // QVGA, CIF: 70% baseline minimum, stepping up with temperature
  COOLING_POLICY_UNIVERSAL,    // Universal: 70% baseline minimum, +5% per 5°C rise
  COOLING_POLICY_SAFE_FALLBACK // Sensor fault fallback
} cooling_policy_t;

void initCooling();
void updateCooling();
void notifyResolutionChange(framesize_t new_framesize);
void setCoolingPausedForOTA(bool paused);
void setCoolingReferenceTemp(float ref_temp_c);
void setCoolingXgaThreshold(float high_temp_c);
void setCoolingPwmFrequency(uint32_t freq_hz);
void setCoolingManualDuty(int16_t duty_pct);
void setCoolingStandbyDuty(uint8_t duty_pct);
void setCoolingMinDuty(uint8_t duty_pct);
void setCoolingTempStep(float step_deg_c);
void setCoolingStepDuty(uint8_t step_pct);
void setCoolingControlMode(uint8_t mode);
void setCoolingPidTargetTemp(float target_c);
void setCoolingKickstartMs(uint16_t ms);
void setCoolingThermalThrottle(bool enabled);

uint8_t getCoolingDutyPercent();
uint32_t getCoolingPwmRaw();
uint32_t getCoolingPwmFrequency();
float getCoolingLastTemperature();
float getCoolingReferenceTemperature();
framesize_t getCoolingActiveFramesize();
cooling_policy_t getCoolingActivePolicy();
const char* getCoolingPolicyName();
const char* getCoolingFramesizeName(framesize_t fs);
void getCoolingDiagnostics(char* buf, size_t len);

#else

inline void initCooling() {}
inline void updateCooling() {}
inline void setCoolingPausedForOTA(bool) {}
inline void getCoolingDiagnostics(char* buf, size_t len) {
  snprintf(buf, len, "\"cooling\":{\"enabled\":false}");
}

#endif
