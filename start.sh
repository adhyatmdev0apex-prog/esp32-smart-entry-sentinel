#!/usr/bin/env bash
set -e
[ -f .env ] || cp .env.example .env
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "[*] Starting Smart Door Sentinel (:5000) & Control Center (:5001) via Docker..."
  docker compose up -d --build
  echo "[+] Dashboard:      http://localhost:5000"
  echo "[+] Control Center: http://localhost:5001"
else
  echo "[*] Docker not found; starting local dual-server (:5000 & :5001)..."
  python3 server.py &
  python3 control_center.py &
  wait
fi
