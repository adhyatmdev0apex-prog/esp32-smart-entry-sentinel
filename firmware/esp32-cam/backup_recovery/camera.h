#pragma once
#include <stddef.h>

bool initializeCamera();
void recordCaptureResult(bool ok, size_t len);
void getCameraDiagnostics(char* buf, size_t len);
