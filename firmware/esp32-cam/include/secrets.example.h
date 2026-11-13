#pragma once

/**
 * ============================================================================
 * SECRETS TEMPLATE (Safe to commit)
 * Copy this file to "secrets.h" and enter your local Wi-Fi credentials.
 * secrets.h is listed in .gitignore to prevent accidental credential commits.
 * ============================================================================
 */

#define SECRET_WIFI_SSID     "YOUR_WIFI_SSID"
#define SECRET_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// Optional static IP configuration (set USE_STATIC_IP to 0 for DHCP):
#define SECRET_USE_STATIC_IP 1
#define SECRET_STATIC_IP     "192.168.29.10"
#define SECRET_GATEWAY_IP    "192.168.29.1"
#define SECRET_SUBNET_MASK   "255.255.255.0"
#define SECRET_DNS_IP        "192.168.29.1"

// Optional custom hardware MAC address override:
#define SECRET_USE_CUSTOM_MAC 1
#define SECRET_CUSTOM_MAC     {0x12, 0x11, 0x11, 0x11, 0x11, 0x11}
