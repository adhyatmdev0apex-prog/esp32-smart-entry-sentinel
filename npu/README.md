# NPU & Hardware Accelerator Support (`npu/`)

This folder isolates all **NPU-specific** and **universal hardware acceleration** logic so the main server remains CPU-first and portable across any machine.

## Execution Priority
1. **CPU (`CPUExecutionProvider`)** — Always available as the server base.
2. **GPU (`CUDAExecutionProvider` / `DmlExecutionProvider`)** — Used automatically when present (within `vlm_engine.py` duty/thermal limits).
3. **NPU (Only when NPU EP is detected):**
   - **AMD XDNA / XDNA 2 NPU (`VitisAIExecutionProvider`)** — Auto-detects `RYZEN_AI_INSTALLATION_PATH` / `C:\Program Files\RyzenAI\` (`vaip_config.json`, `.xclbin`). Requires launching `server.py` from the Ryzen AI Python environment and INT8-quantized `.onnx` models for NPU offload.
   - **Intel NPU (`OpenVINOExecutionProvider`)** — Future universal NPU hook.
   - **Qualcomm Hexagon NPU (`QNNExecutionProvider`)** — Future universal NPU hook.
