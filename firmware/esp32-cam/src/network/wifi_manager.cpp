#include "network/wifi_manager.h"
#include "app_config.h"
#include "core/events.h"
#include "core/logger.h"
#include <WiFi.h>
#include "esp_wifi.h"
#include "esp_netif.h"

extern "C" esp_err_t esp_base_mac_addr_set(const uint8_t *mac);

static bool s_wifi_events_registered = false;
static int s_tx_power_dbm = 18;
static int s_wifi_proto = (WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
static unsigned long s_last_reconnect_attempt = 0;
static unsigned long s_disconnected_since_ms = 0;
static bool s_was_connected = false;
static bool s_fallback_active = false;

static wifi_power_t mapDbmToEnum(int dbm) {
  if (dbm <= 2)  return WIFI_POWER_2dBm;
  if (dbm <= 5)  return WIFI_POWER_5dBm;
  if (dbm <= 7)  return WIFI_POWER_7dBm;
  if (dbm <= 9)  return WIFI_POWER_8_5dBm;
  if (dbm <= 11) return WIFI_POWER_11dBm;
  if (dbm <= 13) return WIFI_POWER_13dBm;
  if (dbm <= 15) return WIFI_POWER_15dBm;
  if (dbm <= 17) return WIFI_POWER_17dBm;
  if (dbm == 18) return WIFI_POWER_18_5dBm;
  if (dbm == 19) return WIFI_POWER_19dBm;
  return WIFI_POWER_19_5dBm;
}

static void applyRadioTuning() {
#if !BOARD_HAS_CAMERA
  // Battery-powered WROOM node: stay associated to Wi-Fi in Modem-Sleep to save power
  WiFi.setSleep(WIFI_PS_MAX_MODEM);
  esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
#else
  // Charger-powered CAM node: zero-latency Wi-Fi for fast wake & MJPEG streaming
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);
#endif
  WiFi.setTxPower(mapDbmToEnum(s_tx_power_dbm));
  if (s_wifi_proto & WIFI_PROTOCOL_11N) {
    esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
  }
}

static void onWiFiEvent(WiFiEvent_t event) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      applyRadioTuning();
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      s_fallback_active = false;
      s_disconnected_since_ms = 0;
#if BOARD_HAS_CAMERA
      if (WiFi.getMode() == WIFI_AP_STA) {
        WiFi.softAPdisconnect(true);
        WiFi.mode(WIFI_STA);
      }
#endif
      applyRadioTuning();
      logInfo("WIFI", "Connected! IP: %s (RSSI: %d dBm, Ch: %d)",
              WiFi.localIP().toString().c_str(), WiFi.RSSI(), WiFi.channel());
      postEvent(EVENT_WIFI_CONNECTED, "WIFI", WiFi.channel(), WiFi.localIP().toString().c_str());
      s_was_connected = true;
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      if (s_disconnected_since_ms == 0) {
        s_disconnected_since_ms = millis();
      }
      if (s_was_connected) {
        logWarn("WIFI", "Connection lost! Waiting to reconnect...");
        postEvent(EVENT_WIFI_DISCONNECTED, "WIFI");
        s_was_connected = false;
      }
      break;
    default:
      break;
  }
}

void initWiFi() {
  if (!s_wifi_events_registered) {
    WiFi.onEvent(onWiFiEvent);
    s_wifi_events_registered = true;
  }

#if SECRET_USE_CUSTOM_MAC
  uint8_t custom_mac[6] = SECRET_CUSTOM_MAC;
  WiFi.mode(WIFI_AP_STA);
  esp_wifi_set_mac(WIFI_IF_STA, custom_mac);
  esp_wifi_set_mac(WIFI_IF_AP, custom_mac);
#endif

  WiFi.mode(WIFI_STA);

#if SECRET_USE_CUSTOM_MAC
  esp_wifi_set_mac(WIFI_IF_STA, custom_mac);
#endif

#if SECRET_USE_STATIC_IP
  IPAddress local_ip, gateway, subnet, dns;
  local_ip.fromString(SECRET_STATIC_IP);
  gateway.fromString(SECRET_GATEWAY_IP);
  subnet.fromString(SECRET_SUBNET_MASK);
  dns.fromString(SECRET_DNS_IP);
  WiFi.config(local_ip, gateway, subnet, dns);
#endif

  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  esp_wifi_set_protocol(WIFI_IF_STA, (uint8_t)s_wifi_proto);
  applyRadioTuning();

  logInfo("WIFI", "Connecting to SSID '%s' (MAC: %s)...", SECRET_WIFI_SSID, WiFi.macAddress().c_str());
  s_disconnected_since_ms = millis();
  WiFi.begin(SECRET_WIFI_SSID, SECRET_WIFI_PASSWORD);
  applyRadioTuning();
}

void updateWiFi() {
  if (WiFi.status() != WL_CONNECTED) {
    unsigned long now = millis();
    if (s_disconnected_since_ms == 0) s_disconnected_since_ms = now;

    if (!s_fallback_active && (now - s_disconnected_since_ms >= 12000)) {
      s_fallback_active = true;
#if BOARD_HAS_CAMERA
      WiFi.mode(WIFI_AP_STA);
      WiFi.softAP("Sentinel-CAM", "sentinel123", 1);
      esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
      logWarn("WIFI", "No Wi-Fi -> Fallback AP 'Sentinel-CAM' (192.168.4.1) + ESP-NOW Ch 1 active");
#else
      WiFi.disconnect(false);
      esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
      logWarn("WIFI", "No Wi-Fi -> ESP-NOW peer fallback Ch 1 active");
#endif
    }

    unsigned long interval = s_fallback_active ? 60000UL : CFG_WIFI_RECONNECT_INTERVAL;
    if (now - s_last_reconnect_attempt >= interval) {
      s_last_reconnect_attempt = now;
      if (!s_fallback_active) {
        logWarn("WIFI", "Reconnecting to Wi-Fi...");
        WiFi.reconnect();
      }
    }
  }
}

bool isWiFiConnected() {
  return (WiFi.status() == WL_CONNECTED);
}

bool isWifiFallbackActive() {
  return s_fallback_active;
}

String getLocalIPString() {
  return WiFi.localIP().toString();
}

int getWiFiRSSI() {
  return WiFi.RSSI();
}

int getWiFiChannel() {
  return WiFi.channel();
}

bool setWiFiTxPower(int dbm_val) {
  s_tx_power_dbm = dbm_val;
  applyRadioTuning();
  return true;
}

int getWiFiTxPower() {
  return s_tx_power_dbm;
}

bool setWiFiProtocol(int proto_bitmap) {
  if (proto_bitmap != 1 && proto_bitmap != 3 && proto_bitmap != 7) return false;
  s_wifi_proto = proto_bitmap;
  esp_err_t err = esp_wifi_set_protocol(WIFI_IF_STA, (uint8_t)s_wifi_proto);
  applyRadioTuning();
  return (err == ESP_OK);
}

int getWiFiProtocol() {
  return s_wifi_proto;
}

void getWiFiDiagnosticsJson(char* buf, size_t max_len) {
  snprintf(buf, max_len,
           "\"ip\":\"%s\",\"rssi\":%d,\"wifi_ch\":%d,\"tx_pwr\":%d,\"wifi_proto\":%d",
           WiFi.localIP().toString().c_str(),
           (int)WiFi.RSSI(),
           (int)WiFi.channel(),
           s_tx_power_dbm,
           s_wifi_proto);
}
