#pragma once
#include <Arduino.h>

/**
 * ============================================================================
 * WI-FI CONNECTION MANAGER
 * Handles connection, non-blocking reconnect, power tuning, and Wi-Fi events.
 * ============================================================================
 */

void initWiFi();
void updateWiFi();

bool isWiFiConnected();
bool isWifiFallbackActive();
void notifyServerHeartbeat();
String getLocalIPString();
int getWiFiRSSI();
int getWiFiChannel();

bool setWiFiTxPower(int dbm_val);
int getWiFiTxPower();

bool setWiFiProtocol(int proto_bitmap);
int getWiFiProtocol();

void getWiFiDiagnosticsJson(char* buf, size_t max_len);
