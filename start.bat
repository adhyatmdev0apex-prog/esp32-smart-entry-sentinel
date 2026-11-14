@echo off
if not exist .env copy .env.example .env >nul
where docker >nul 2>&1
if %ERRORLEVEL%==0 (
    echo [*] Starting Smart Door Sentinel (:5000) and Control Center (:5001) via Docker...
    docker compose up -d --build
    echo [+] Dashboard:      http://localhost:5000
    echo [+] Control Center: http://localhost:5001
) else (
    echo [*] Starting local dual-server (:5000 and :5001)...
    start "Sentinel :5000" python server.py
    start "Control Center :5001" python control_center.py
)
