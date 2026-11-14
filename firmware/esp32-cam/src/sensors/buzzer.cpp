#include "sensors/buzzer.h"
#include "app_config.h"

#if HAS_BUZZER
static constexpr uint8_t BUZZER_LEDC_CH = 4;         // Timer 2 (avoids CAM Timer 0 & Cooling Timer 1)
static constexpr uint8_t BUZZER_LEDC_RES = 10;       // 10-bit PWM (0..1023)
static constexpr uint32_t BUZZER_DRIVE_DUTY = 360;   // ~35% duty: keeps warm fundamental without piezo rattle
static constexpr uint16_t BUZZER_FREQ_MED_HZ = 1000; // Upper tone (> 900 Hz)
static constexpr uint16_t BUZZER_FREQ_LOW_HZ = 910;  // Lower tone (> 900 Hz)
static constexpr uint16_t BUZZER_STEP_MS = 500;      // 0.5s alternating cadence

static int s_buzzer_pin = PIN_DEFAULT_BUZZER;
static bool s_is_beeping = false;
static bool s_two_tone_active = false;
static bool s_two_tone_med = true;
static uint32_t s_beep_end_ms = 0;
static uint32_t s_two_tone_end_ms = 0;
static uint32_t s_two_tone_step_ms = 0;
static uint8_t s_remaining_beeps = 0;
static uint16_t s_beep_duration = 100;

static void playBuzzerTone(uint16_t freq_hz)
{
  ledcWriteTone(BUZZER_LEDC_CH, freq_hz);
  ledcWrite(BUZZER_LEDC_CH, BUZZER_DRIVE_DUTY);
}

static void stopBuzzerOutput()
{
  ledcWrite(BUZZER_LEDC_CH, 0);
  ledcWriteTone(BUZZER_LEDC_CH, 0);
}

void initBuzzer()
{
  ledcSetup(BUZZER_LEDC_CH, BUZZER_FREQ_MED_HZ, BUZZER_LEDC_RES);
  ledcAttachPin(s_buzzer_pin, BUZZER_LEDC_CH);
  stopBuzzerOutput();
}

void updateBuzzer()
{
  uint32_t now = millis();

  if (s_two_tone_active)
  {
    if ((int32_t)(now - s_two_tone_end_ms) >= 0)
    {
      stopBuzzerOutput();
      s_two_tone_active = false;
      return;
    }
    if ((int32_t)(now - s_two_tone_step_ms) >= 0)
    {
      s_two_tone_med = !s_two_tone_med;
      playBuzzerTone(s_two_tone_med ? BUZZER_FREQ_MED_HZ : BUZZER_FREQ_LOW_HZ);
      s_two_tone_step_ms = now + BUZZER_STEP_MS;
    }
    return;
  }

  if (s_is_beeping && (int32_t)(now - s_beep_end_ms) >= 0)
  {
    stopBuzzerOutput();
    s_is_beeping = false;
    if (s_remaining_beeps > 0)
    {
      s_remaining_beeps--;
      delay(50);
      playBuzzerTone(BUZZER_FREQ_MED_HZ);
      s_is_beeping = true;
      s_beep_end_ms = millis() + s_beep_duration;
    }
  }
}

void beep(uint16_t duration_ms, uint8_t count)
{
  if (count == 0)
    return;
  s_two_tone_active = false;
  s_beep_duration = duration_ms;
  s_remaining_beeps = count - 1;
  playBuzzerTone(BUZZER_FREQ_MED_HZ);
  s_is_beeping = true;
  s_beep_end_ms = millis() + duration_ms;
}

void playTwoToneAlert(uint16_t total_duration_ms)
{
  if (total_duration_ms == 0)
    return;
  s_is_beeping = false;
  s_remaining_beeps = 0;
  uint32_t now = millis();
  s_two_tone_active = true;
  s_two_tone_med = true;
  s_two_tone_end_ms = now + total_duration_ms;
  s_two_tone_step_ms = now + BUZZER_STEP_MS;
  playBuzzerTone(BUZZER_FREQ_MED_HZ);
}

#else

void initBuzzer() {}
void updateBuzzer() {}
void beep(uint16_t duration_ms, uint8_t count) {}
void playTwoToneAlert(uint16_t total_duration_ms) {}

#endif
