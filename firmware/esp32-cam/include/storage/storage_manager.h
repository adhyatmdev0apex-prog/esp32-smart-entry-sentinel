#pragma once
#include <Arduino.h>

void initStorage();
bool isStorageMounted();
bool saveSnapshotToStorage(const uint8_t* jpeg_data, size_t length, const char* label = "snap");
void getStorageDiagnosticsJson(char* buf, size_t max_len);
