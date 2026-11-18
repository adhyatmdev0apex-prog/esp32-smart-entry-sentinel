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
static unsigned long s_last_server_heartbeat_ms = 0;
static bool s_was_connected = false;
static bool s_fallback_active = false;
static bool s_server_ap_active = false;
static uint8_t s_sim_mode = 0; // 0 = Normal, 1 = Forced Blackout/AP+ESP-NOW, 2 = Forced Dual AP+STA
static uint32_t s_reconnect_count = 0;

void notifyServerHeartbeat() {
  s_last_server_heartbeat_ms = millis();
}

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
  // Use full power while connecting; switch to MIN_MODEM once IP is obtained
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(WIFI_PS_MIN_MODEM);
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
  } else {
    WiFi.setSleep(false);
    esp_wifi_set_ps(WIFI_PS_NONE);
  }
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
      s_reconnect_count++;
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
  unsigned long now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (s_disconnected_since_ms == 0) s_disconnected_since_ms = now;

    if (!s_fallback_active && (now - s_disconnected_since_ms >= 20000)) {
      s_fallback_active = true;
#if BOARD_HAS_CAMERA
      WiFi.mode(WIFI_AP_STA);
      WiFi.softAP(SECRET_AP_SSID, SECRET_AP_PASSWORD, 1);
      s_server_ap_active = true;
      esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
      logWarn("WIFI", "No Wi-Fi -> Fallback AP '%s' (192.168.4.1) + ESP-NOW Ch 1 active", SECRET_AP_SSID);
#else
      esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
      logWarn("WIFI", "No Wi-Fi -> ESP-NOW peer fallback Ch 1 active");
#endif
    }

    unsigned long interval = s_fallback_active ? 30000UL : CFG_WIFI_RECONNECT_INTERVAL;
    if (now - s_last_reconnect_attempt >= interval) {
      s_last_reconnect_attempt = now;
      logWarn("WIFI", "Reconnecting to Wi-Fi...");
      WiFi.begin(SECRET_WIFI_SSID, SECRET_WIFI_PASSWORD);
    }
  } else {
#if BOARD_HAS_CAMERA
    // Even when Wi-Fi STA is connected, start Fallback AP if dashboard server is unreachable (>15s) or sim mode active
    bool server_alive = (s_last_server_heartbeat_ms > 0) && ((now - s_last_server_heartbeat_ms) < 15000UL);
    if (s_sim_mode == 0) {
      if (!server_alive && now >= 15000UL && !s_server_ap_active) {
        s_server_ap_active = true;
        WiFi.mode(WIFI_AP_STA);
        WiFi.softAP(SECRET_AP_SSID, SECRET_AP_PASSWORD);
        logWarn("WIFI", "Server unreachable -> Fallback AP '%s' (192.168.4.1) started", SECRET_AP_SSID);
      } else if (server_alive && s_server_ap_active) {
        s_server_ap_active = false;
        WiFi.softAPdisconnect(true);
        WiFi.mode(WIFI_STA);
        logInfo("WIFI", "Server reachable -> Fallback AP stopped");
      }
    }
#endif
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

int getWiFiSignalQualityPct() {
  if (WiFi.status() != WL_CONNECTED) return 0;
  int rssi = WiFi.RSSI();
  if (rssi <= -100) return 0;
  if (rssi >= -50) return 100;
  return 2 * (rssi + 100);
}

bool setWiFiSimMode(int mode) {
  if (mode < 0 || mode > 2) return false;
  s_sim_mode = (uint8_t)mode;
  if (mode == 0) {
    s_fallback_active = false;
    s_server_ap_active = false;
#if BOARD_HAS_CAMERA
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_STA);
#endif
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.begin(SECRET_WIFI_SSID, SECRET_WIFI_PASSWORD);
    }
    notifyServerHeartbeat();
    logInfo("WIFI", "SIM: Restored Normal STA mode");
  } else if (mode == 1) {
    s_fallback_active = true;
#if BOARD_HAS_CAMERA
    s_server_ap_active = true;
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(SECRET_AP_SSID, SECRET_AP_PASSWORD, 1);
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
    logWarn("WIFI", "SIM: Forced Router Blackout -> Fallback AP '%s' (192.168.4.1) + ESP-NOW Ch 1", SECRET_AP_SSID);
#else
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
    logWarn("WIFI", "SIM: Forced ESP-NOW Ch 1 fallback mode");
#endif
  } else if (mode == 2) {
#if BOARD_HAS_CAMERA
    s_server_ap_active = true;
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(SECRET_AP_SSID, SECRET_AP_PASSWORD);
    logWarn("WIFI", "SIM: Forced Dual AP+STA mode ('%s' @ 192.168.4.1)", SECRET_AP_SSID);
#endif
  }
  return true;
}

int getWiFiSimMode() {
  return (int)s_sim_mode;
}

void getWiFiDiagnosticsJson(char* buf, size_t max_len) {
  unsigned long now = millis();
  unsigned long hb_age_ms = (s_last_server_heartbeat_ms > 0 && now >= s_last_server_heartbeat_ms)
                              ? (now - s_last_server_heartbeat_ms)
                              : 0;
  bool sta_ok = (WiFi.status() == WL_CONNECTED);
  bool ap_on = s_server_ap_active || (WiFi.getMode() == WIFI_AP) || (WiFi.getMode() == WIFI_AP_STA);
  snprintf(buf, max_len,
           "\"ip\":\"%s\",\"ap_ip\":\"%s\",\"sta_connected\":%s,\"ap_active\":%s,"
           "\"ap_ssid\":\"%s\",\"ap_clients\":%d,\"espnow_fallback\":%s,\"wifi_sim_mode\":%u,"
           "\"rssi\":%d,\"signal_quality_pct\":%d,\"wifi_ch\":%d,\"tx_pwr\":%d,\"wifi_proto\":%d,"
           "\"reconnect_count\":%lu,\"last_server_hb_age_ms\":%lu",
           WiFi.localIP().toString().c_str(),
           ap_on ? WiFi.softAPIP().toString().c_str() : "0.0.0.0",
           sta_ok ? "true" : "false",
           ap_on ? "true" : "false",
           SECRET_AP_SSID,
           ap_on ? (int)WiFi.softAPgetStationNum() : 0,
           s_fallback_active ? "true" : "false",
           (unsigned int)s_sim_mode,
           sta_ok ? (int)WiFi.RSSI() : -100,
           getWiFiSignalQualityPct(),
           (int)WiFi.channel(),
           s_tx_power_dbm,
           s_wifi_proto,
           (unsigned long)s_reconnect_count,
           hb_age_ms);
}
