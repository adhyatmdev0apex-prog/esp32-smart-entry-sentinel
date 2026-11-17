#include "camera/cooling.h"
#include "camera/camera_manager.h"
#include "app_config.h"
#include "core/logger.h"

#if HAS_CAMERA_FEATURE
#include <esp_log.h>
#include <driver/gpio.h>
#include <math.h>

static framesize_t s_active_framesize = FRAMESIZE_INVALID;
static cooling_policy_t s_active_policy = COOLING_POLICY_UNIVERSAL;

static float s_ref_temp_c = CFG_COOLING_REF_TEMP_C;
static float s_xga_high_temp_threshold_c = CFG_COOLING_XGA_THRESH_C;
static uint32_t s_pwm_freq_hz = CFG_COOLING_PWM_FREQ_HZ;

static uint8_t s_target_duty_pct = CFG_COOLING_MIN_DUTY_PCT;
static uint8_t s_current_duty_pct = 0;
static uint32_t s_applied_pwm_raw = 0;
static float s_last_measured_temp_c = 0.0f;
static bool s_last_temp_valid = false;

static uint32_t s_last_eval_ms = 0;
static bool s_kickstart_active = false;
static uint32_t s_kickstart_end_ms = 0;

static uint8_t s_last_logged_duty = 255;
static cooling_policy_t s_last_logged_policy = (cooling_policy_t)255;
static float s_last_logged_temp = -999.0f;

static bool s_paused_for_ota = false;
static bool s_initialized = false;

const char* getCoolingFramesizeName(framesize_t fs) {
  switch (fs) {
    case FRAMESIZE_96X96:   return "96x96";
    case FRAMESIZE_QQVGA:   return "QQVGA (160x120)";
    case FRAMESIZE_QCIF:    return "QCIF (176x144)";
    case FRAMESIZE_HQVGA:   return "HQVGA (240x176)";
    case FRAMESIZE_240X240: return "240x240";
    case FRAMESIZE_QVGA:    return "QVGA (320x240)";
    case FRAMESIZE_CIF:     return "CIF (400x296)";
    case FRAMESIZE_HVGA:    return "HVGA (480x320)";
    case FRAMESIZE_VGA:     return "VGA (640x480)";
    case FRAMESIZE_SVGA:    return "SVGA (800x600)";
    case FRAMESIZE_XGA:     return "XGA (1024x768)";
    case FRAMESIZE_HD:      return "HD (1280x720)";
    case FRAMESIZE_SXGA:    return "SXGA (1280x1024)";
    case FRAMESIZE_UXGA:    return "UXGA (1600x1200)";
    default:                return "CUSTOM/UNKNOWN";
  }
}

const char* getCoolingPolicyName() {
  switch (s_active_policy) {
    case COOLING_POLICY_FIXED_MAX:    return "FIXED_100_PERCENT";
    case COOLING_POLICY_XGA:          return "XGA_TEMP_SENSITIVE";
    case COOLING_POLICY_LOW_POWER:    return "LOW_POWER_QVGA_CIF";
    case COOLING_POLICY_UNIVERSAL:    return "UNIVERSAL_STEPPING";
    case COOLING_POLICY_SAFE_FALLBACK:return "SAFE_FALLBACK";
    default:                          return "UNKNOWN";
  }
}

static cooling_policy_t determinePolicyForFramesize(framesize_t fs) {
  switch (fs) {
    case FRAMESIZE_HD:
    case FRAMESIZE_SXGA:
    case FRAMESIZE_UXGA:
      return COOLING_POLICY_FIXED_MAX;
    case FRAMESIZE_XGA:
      return COOLING_POLICY_XGA;
    case FRAMESIZE_QVGA:
    case FRAMESIZE_CIF:
      return COOLING_POLICY_LOW_POWER;
    default:
      return COOLING_POLICY_UNIVERSAL;
  }
}

static void applyPwmDuty(uint8_t duty_pct) {
  if (s_paused_for_ota || duty_pct == 0) {
    s_applied_pwm_raw = 0;
    ledcWrite(COOLING_LEDC_CH, 0);
    digitalWrite(PIN_COOLING_FAN, LOW);
    s_current_duty_pct = 0;
    return;
  }

  // Absolute minimum speed clamp (50% floor)
  if (duty_pct < CFG_COOLING_MIN_DUTY_PCT) duty_pct = CFG_COOLING_MIN_DUTY_PCT;
  if (duty_pct > 100) duty_pct = 100;

  uint32_t raw = (uint32_t)((duty_pct * 255 + 50) / 100);
  s_applied_pwm_raw = raw;
  ledcWrite(COOLING_LEDC_CH, raw);
  s_current_duty_pct = duty_pct;
}

static uint8_t calculateDutyForPolicy(cooling_policy_t policy, float temp_c, bool temp_valid) {
  if (policy == COOLING_POLICY_FIXED_MAX) {
    return 100;
  }

  if (!temp_valid) {
    if (policy == COOLING_POLICY_XGA) return 100;
    else if (policy == COOLING_POLICY_LOW_POWER) return CFG_COOLING_MIN_DUTY_PCT;
    else return 75;
  }

  float rise = temp_c - s_ref_temp_c;
  int steps = 0;
  if (rise > 0.0f) {
    steps = (int)(rise / CFG_COOLING_TEMP_STEP_DEG_C);
  }
  int step_increase = steps * CFG_COOLING_STEP_DUTY_PCT;

  if (policy == COOLING_POLICY_XGA) {
    if (temp_c >= s_xga_high_temp_threshold_c) return 100;
    int duty = 75 + step_increase;
    if (duty < 75) duty = 75;
    if (duty > 100) duty = 100;
    return (uint8_t)duty;
  }

  if (policy == COOLING_POLICY_LOW_POWER) {
    int duty = CFG_COOLING_MIN_DUTY_PCT + step_increase;
    if (duty < CFG_COOLING_MIN_DUTY_PCT) duty = CFG_COOLING_MIN_DUTY_PCT;
    if (duty > 100) duty = 100;
    return (uint8_t)duty;
  }

  int duty = CFG_COOLING_MIN_DUTY_PCT + step_increase;
  if (duty < CFG_COOLING_MIN_DUTY_PCT) duty = CFG_COOLING_MIN_DUTY_PCT;
  if (duty > 100) duty = 100;
  return (uint8_t)duty;
}

static void evaluateCooling(bool force_reeval) {
  if (s_paused_for_ota || !s_initialized) return;

  uint32_t now = millis();
  if (!force_reeval && (now - s_last_eval_ms < 1000)) {
    if (s_kickstart_active && now >= s_kickstart_end_ms) {
      s_kickstart_active = false;
      applyPwmDuty(s_target_duty_pct);
      logInfo("COOLING", "Blower kickstart complete. Set to target: %d%%", s_target_duty_pct);
    }
    return;
  }
  s_last_eval_ms = now;

  sensor_t* s = esp_camera_sensor_get();
  if (s != NULL && s->status.framesize != s_active_framesize) {
    s_active_framesize = (framesize_t)s->status.framesize;
    s_active_policy = determinePolicyForFramesize(s_active_framesize);
  }

  float raw_temp = temperatureRead();
  bool temp_valid = (!isnan(raw_temp) && raw_temp >= -10.0f && raw_temp <= 125.0f);
  s_last_measured_temp_c = raw_temp;
  s_last_temp_valid = temp_valid;

  uint8_t target_duty = calculateDutyForPolicy(s_active_policy, raw_temp, temp_valid);
  float cal_temp = raw_temp - 26.0f;
  if (!isCameraSensorActive() && temp_valid && cal_temp < 52.0f) {
    target_duty = 0; // Silent fan during Low-Power Standby
  } else if (temp_valid && cal_temp > 78.0f) {
    target_duty = 100;
    logWarn("COOLING", "Thermal guard: high die temp %.1f°C -> fan 100%%", cal_temp);
  }
  s_target_duty_pct = target_duty;

  if (s_current_duty_pct == 0 && target_duty > 0 && target_duty < 100) {
    s_kickstart_active = true;
    s_kickstart_end_ms = now + 250;
    applyPwmDuty(100);
    logInfo("COOLING", "Kickstarting blower at 100%% for 250ms before applying %d%%", target_duty);
  } else if (!s_kickstart_active) {
    applyPwmDuty(target_duty);
  }

  bool duty_changed = (s_current_duty_pct != s_last_logged_duty);
  bool policy_changed = (s_active_policy != s_last_logged_policy);
  bool temp_shifted = (fabsf(raw_temp - s_last_logged_temp) >= 2.0f);

  if (duty_changed || policy_changed || temp_shifted) {
    logInfo("COOLING", "Res: %s | Policy: %s | Temp: %.1f°C | Fan: %d%% (PWM: %u/255, %u Hz)",
            getCoolingFramesizeName(s_active_framesize),
            getCoolingPolicyName(),
            s_last_measured_temp_c,
            s_current_duty_pct,
            (unsigned int)s_applied_pwm_raw,
            (unsigned int)s_pwm_freq_hz);
    s_last_logged_duty = s_current_duty_pct;
    s_last_logged_policy = s_active_policy;
    s_last_logged_temp = raw_temp;
  }
}

void initCooling() {
  pinMode(PIN_COOLING_FAN, OUTPUT);
  digitalWrite(PIN_COOLING_FAN, LOW);
  gpio_set_pull_mode((gpio_num_t)PIN_COOLING_FAN, GPIO_PULLDOWN_ONLY);

  ledcSetup(COOLING_LEDC_CH, s_pwm_freq_hz, 8);
  ledcAttachPin(PIN_COOLING_FAN, COOLING_LEDC_CH);
  ledcWrite(COOLING_LEDC_CH, 0);

  s_initialized = true;

  sensor_t* s = esp_camera_sensor_get();
  s_active_framesize = s ? (framesize_t)s->status.framesize : FRAMESIZE_SVGA;
  s_active_policy = determinePolicyForFramesize(s_active_framesize);

  logInfo("COOLING", "Initialized on GPIO %d (LEDC Ch %d, %u Hz). Policy: %s",
          PIN_COOLING_FAN, COOLING_LEDC_CH, (unsigned int)s_pwm_freq_hz, getCoolingPolicyName());

  evaluateCooling(true);
}

void updateCooling() {
  evaluateCooling(false);
}

void notifyResolutionChange(framesize_t new_framesize) {
  if (new_framesize == s_active_framesize && s_initialized) return;
  s_active_framesize = new_framesize;
  s_active_policy = determinePolicyForFramesize(new_framesize);
  evaluateCooling(true);
}

void setCoolingPausedForOTA(bool paused) {
  s_paused_for_ota = paused;
  if (paused) {
    s_kickstart_active = false;
    applyPwmDuty(0);
    digitalWrite(PIN_COOLING_FAN, LOW);
    logInfo("COOLING", "Fan paused for OTA update safety");
  } else {
    evaluateCooling(true);
  }
}

void setCoolingReferenceTemp(float ref_temp_c) {
  if (ref_temp_c >= 20.0f && ref_temp_c <= 90.0f) {
    s_ref_temp_c = ref_temp_c;
    evaluateCooling(true);
  }
}

void setCoolingXgaThreshold(float high_temp_c) {
  if (high_temp_c >= 50.0f && high_temp_c <= 95.0f) {
    s_xga_high_temp_threshold_c = high_temp_c;
    evaluateCooling(true);
  }
}

void setCoolingPwmFrequency(uint32_t freq_hz) {
  if (freq_hz >= 10 && freq_hz <= 50000) {
    s_pwm_freq_hz = freq_hz;
    ledcSetup(COOLING_LEDC_CH, s_pwm_freq_hz, 8);
    applyPwmDuty(s_current_duty_pct);
  }
}

uint8_t getCoolingDutyPercent() { return s_current_duty_pct; }
uint32_t getCoolingPwmRaw() { return s_applied_pwm_raw; }
uint32_t getCoolingPwmFrequency() { return s_pwm_freq_hz; }
float getCoolingLastTemperature() { return s_last_measured_temp_c; }
float getCoolingReferenceTemperature() { return s_ref_temp_c; }
framesize_t getCoolingActiveFramesize() { return s_active_framesize; }
cooling_policy_t getCoolingActivePolicy() { return s_active_policy; }

void getCoolingDiagnostics(char* buf, size_t len) {
  snprintf(buf, len,
           "\"fan_gpio\":%d,\"fan_pct\":%u,\"fan_pwm\":%u,\"fan_min_pct\":%u,\"fan_freq\":%u,\"fan_policy\":\"%s\","
           "\"fan_ref_c\":%.1f,\"fan_xga_thresh_c\":%.1f,\"fan_kick\":%s",
           PIN_COOLING_FAN,
           (unsigned int)s_current_duty_pct,
           (unsigned int)s_applied_pwm_raw,
           (unsigned int)CFG_COOLING_MIN_DUTY_PCT,
           (unsigned int)s_pwm_freq_hz,
           getCoolingPolicyName(),
           s_ref_temp_c,
           s_xga_high_temp_threshold_c,
           s_kickstart_active ? "true" : "false");
}

#endif
