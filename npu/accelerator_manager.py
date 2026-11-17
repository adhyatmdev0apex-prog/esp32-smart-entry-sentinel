"""
Universal Hardware Accelerator Selector
Priority:
  1. CPU (always present as guaranteed server base)
  2. GPU (CUDA / DirectML when enabled & within duty limits)
  3. NPU (AMD XDNA/XDNA2 VitisAI, Intel OpenVINO NPU, Qualcomm QNN) — ONLY if detected
"""

from .amd_xdna2 import detect_amd_ryzen_ai


def get_execution_providers(available_providers, use_gpu=True, use_npu=True):
    """Build ordered ONNX Runtime ExecutionProvider list with CPU as guaranteed base."""
    avail = list(available_providers or [])
    providers = []
    active_target = "CPU"

    if use_npu:
        if "VitisAIExecutionProvider" in avail:
            amd_info = detect_amd_ryzen_ai()
            vaip_cfg = amd_info.get("vaip_config")
            providers.append(
                ("VitisAIExecutionProvider", {"config_file": vaip_cfg})
                if vaip_cfg else "VitisAIExecutionProvider"
            )
            active_target = "AMD XDNA/XDNA2 NPU (VitisAI)"
        elif "QNNExecutionProvider" in avail:
            providers.append("QNNExecutionProvider")
            active_target = "Qualcomm NPU (QNN)"
        elif "OpenVINOExecutionProvider" in avail:
            providers.append(("OpenVINOExecutionProvider", {"device_type": "NPU"}))
            active_target = "Intel NPU (OpenVINO)"

    if use_gpu:
        if "CUDAExecutionProvider" in avail:
            providers.append("CUDAExecutionProvider")
            if active_target == "CPU":
                active_target = "NVIDIA GPU (CUDA)"
        elif "DmlExecutionProvider" in avail:
            providers.append("DmlExecutionProvider")
            if active_target == "CPU":
                active_target = "GPU (DirectML)"

    providers.append("CPUExecutionProvider")
    return providers, active_target


def get_accelerator_summary(available_providers):
    """Return human-readable accelerator status for diagnostics & UI."""
    providers, active_target = get_execution_providers(available_providers)
    return {
        "active_target": active_target,
        "available_providers": list(available_providers or []),
        "configured_providers": [p[0] if isinstance(p, tuple) else p for p in providers],
        "amd_ryzen_ai": detect_amd_ryzen_ai(),
    }
