#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/data /app/entrance_monitoring /app/models /app/workspace /app/releases /app/builds /app/config

# Optional local credential injection from .env / Docker environment
python3 - <<'PY'
import os, re

sec_dir = "/app/workspace/firmware/esp32-cam/include"
os.makedirs(sec_dir, exist_ok=True)
repo_sec = "/app/firmware/esp32-cam/include/secrets.h"
ws_sec = os.path.join(sec_dir, "secrets.h")

if not os.path.exists(ws_sec) and os.path.exists(repo_sec):
    with open(repo_sec, "r", encoding="utf-8") as f:
        text = f.read()
    with open(ws_sec, "w", encoding="utf-8") as f:
        f.write(text)

ssid = os.environ.get("WIFI_SSID", "").strip()
wpw = os.environ.get("WIFI_PASSWORD", "").strip()
if ssid and wpw:
    wifi_h = f'#pragma once\n#define WIFI_SSID "{ssid}"\n#define WIFI_PASSWORD "{wpw}"\n'
    for p in ("/app/firmware/esp32-cam/include/wifi_secrets.h", os.path.join(sec_dir, "wifi_secrets.h")):
        with open(p, "w", encoding="utf-8") as f:
            f.write(wifi_h)

admin_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
user_pw = os.environ.get("USER_PASSWORD", "").strip()
if admin_pw or user_pw:
    for p in (repo_sec, ws_sec):
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                c = f.read()
            if admin_pw:
                c = re.sub(r'(#define\s+SECRET_ADMIN_PASSWORD\s+)"[^"]*"', rf'\1"{admin_pw}"', c)
            if user_pw:
                c = re.sub(r'(#define\s+SECRET_VIEW_PASSWORD\s+)"[^"]*"', rf'\1"{user_pw}"', c)
            with open(p, "w", encoding="utf-8") as f:
                f.write(c)
PY

python3 /app/server.py &
PID_5000=$!
python3 /app/control_center.py &
PID_5001=$!

trap "kill $PID_5000 $PID_5001 2>/dev/null || true; exit 0" SIGINT SIGTERM
wait -n $PID_5000 $PID_5001
exit $?
