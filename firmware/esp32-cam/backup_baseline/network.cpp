#include <Arduino.h>
#include <WiFi.h>
#include "esp_wifi.h"
#include "esp_netif.h"

#include "network.h"
#include "wifi_config.h"

void connectToWiFi()
{
  WiFi.mode(_STA);

  // 1. Force MAC address for both STA and AP interfaces to 12:11:11:11:11:11
  uint8_t custom_mac[6] = {0x12, 0x11, 0x11, 0x11, 0x11, 0x11};
  esp_wifi_set_mac(WIFI_IF_STA, custom_mac);
  esp_wifi_set_mac(WIFI_IF_AP, custom_mac);

  // Switch to STA mode for client connection
  WiFi.mode(WIFI_STA);
  esp_wifi_set_mac(WIFI_IF_STA, custom_mac);

  // 2. Lock IP to 192.168.29.10
  IPAddress local_IP(192, 168, 29, 10);
  IPAddress gateway(192, 168, 29, 1);
  IPAddress subnet(255, 255, 255, 0);
  IPAddress primaryDNS(192, 168, 29, 1);

  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS))
  {
    Serial.println("[WIFI] STA Failed to configure static IP");
  }

  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);

  Serial.printf("[WIFI] Hardcoded MAC: %s\n", WiFi.macAddress().c_str());
  Serial.printf("[WIFI] Target IP: 192.168.29.10\n");
  Serial.printf("[WIFI] Connecting to '%s'...\n", WIFI_SSID);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start < 25000))
  {
    delay(400);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED)
  {
    WiFi.setSleep(false);
    esp_wifi_set_ps(WIFI_PS_NONE);
    Serial.println("\n[WIFI] Connected successfully!");
    Serial.printf("[WIFI] IP: %s | MAC: %s | RSSI: %d dBm\n",
                  WiFi.localIP().toString().c_str(),
                  WiFi.macAddress().c_str(),
                  WiFi.RSSI());
  }
  else
  {
    Serial.printf("\n[WIFI] Connect failed, status: %d\n", WiFi.status());
  }
}
