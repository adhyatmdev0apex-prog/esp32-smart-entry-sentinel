@echo off
setlocal
set "RYZEN_AI_INSTALLATION_PATH=C:\Program Files\RyzenAI\1.9.0"
set "PYTHONPATH=%~dp0.npu_pkgs;%~dp0"
echo [*] Launching Sentinel Server on AMD Ryzen AI 1.9.0 (XDNA 2 NPU + Hybrid 90:10)...
"C:\Program Files\RyzenAI\ryzen-ai-1.9.0\Scripts\python.exe" "%~dp0server.py"
endlocal
