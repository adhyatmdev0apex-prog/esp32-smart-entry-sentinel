#include "sensors/buzzer.h"
#include "app_config.h"

#if HAS_BUZZER
static int s_buzzer_pin = PIN_DEFAULT_BUZZER;
static bool s_is_beeping = false;
static uint32_t s_beep_end_ms = 0;
static uint8_t s_remaining_beeps = 0;
static uint16_t s_beep_duration = 100;

void initBuzzer() {
  pinMode(s_buzzer_pin, OUTPUT);
  digitalWrite(s_buzzer_pin, !CFG_BUZZER_ACTIVE_LEVEL);
}

void updateBuzzer() {
  uint32_t now = millis();
  if (s_is_beeping && now >= s_beep_end_ms) {
    digitalWrite(s_buzzer_pin, !CFG_BUZZER_ACTIVE_LEVEL);
    s_is_beeping = false;
    if (s_remaining_beeps > 0) {
      s_remaining_beeps--;
      delay(50);
      digitalWrite(s_buzzer_pin, CFG_BUZZER_ACTIVE_LEVEL);
      s_is_beeping = true;
      s_beep_end_ms = millis() + s_beep_duration;
    }
  }
}

void beep(uint16_t duration_ms, uint8_t count) {
  if (count == 0) return;
  s_beep_duration = duration_ms;
  s_remaining_beeps = count - 1;
  digitalWrite(s_buzzer_pin, CFG_BUZZER_ACTIVE_LEVEL);
  s_is_beeping = true;
  s_beep_end_ms = millis() + duration_ms;
}

#else

void initBuzzer() {}
void updateBuzzer() {}
void beep(uint16_t duration_ms, uint8_t count) {}

#endif
