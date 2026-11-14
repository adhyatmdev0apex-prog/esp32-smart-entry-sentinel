#pragma once

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#define SECRET_WIFI_SSID WIFI_SSID
#define SECRET_WIFI_PASSWORD WIFI_PASSWORD
#else
#define SECRET_WIFI_SSID "YOUR_WIFI_SSID"
#define SECRET_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#endif

// Static IP & MAC assignments per board
#define SECRET_USE_STATIC_IP 1
#define SECRET_GATEWAY_IP "192.168.29.1"
#define SECRET_SUBNET_MASK "255.255.255.0"
#define SECRET_DNS_IP "192.168.29.1"

#if defined(BOARD_ESP32_WROOM)
// ESP32-WROOM Door & Sensor Monitor
#define SECRET_STATIC_IP "192.168.29.20"
#define SECRET_USE_CUSTOM_MAC 1
#define SECRET_CUSTOM_MAC {0x12, 0x11, 0x11, 0x11, 0x11, 0x20}
#else
// AI-Thinker ESP32-CAM (Video Stream Server on Port 81)
#define SECRET_STATIC_IP "192.168.29.10"
#define SECRET_USE_CUSTOM_MAC 1
#define SECRET_CUSTOM_MAC {0x12, 0x11, 0x11, 0x11, 0x11, 0x11}
#endif

// Dashboard Web Login Credentials (used by server.py)
// Edit passwords below manually; takes effect on next login.
#define SECRET_ADMIN_USER "ADmin"
#define SECRET_ADMIN_PASSWORD "2026@ADmin"
#define SECRET_VIEW_USER "user"
#define SECRET_VIEW_PASSWORD "user@123"
