"""
Universal Hardware Accelerator Selector
Priority:
  1. CPU (always present as guaranteed server base)
  2. GPU (CUDA / DirectML when enabled & within duty limits)
  3. NPU (AMD XDNA/XDNA2 VitisAI, Intel OpenVINO NPU, Qualcomm QNN) — ONLY if detected
"""

import os
from .amd_xdna2 import detect_amd_ryzen_ai


def get_execution_providers(available_providers, use_gpu=True, use_npu=True, npu_only=None, mode=None):
    """Build ordered ONNX Runtime ExecutionProvider list with CPU as guaranteed base (unless npu_only=True).
    mode: 'npu' | 'gpu' | 'hybrid_90_10' | None (auto)
    """
    if npu_only is None:
        npu_only = os.environ.get("SENTINEL_NPU_ONLY", "0").strip().lower() in ("1", "true", "yes")
    if mode == "gpu":
        use_npu = False
        use_gpu = True
        npu_only = False
    elif mode == "npu":
        use_npu = True
        use_gpu = False
    elif mode == "hybrid_90_10":
        # Never mix VitisAI and Dml in the same session (prevents 50/50 subgraph fallback to GPU).
        # Caller routes continuous heavy detection (~90%) to NPU+CPU and occasional face/secondary (~10%) to GPU.
        use_npu = True
        use_gpu = False
    avail = list(available_providers or [])
    providers = []
    active_target = "CPU"

    if use_npu or npu_only:
        if "VitisAIExecutionProvider" in avail:
            amd_info = detect_amd_ryzen_ai()
            if amd_info.get("install_dir") and not os.environ.get("RYZEN_AI_INSTALLATION_PATH"):
                os.environ["RYZEN_AI_INSTALLATION_PATH"] = amd_info["install_dir"]
            providers.append("VitisAIExecutionProvider")
            active_target = "AMD XDNA/XDNA2 NPU (VitisAI)"
        elif "QNNExecutionProvider" in avail:
            providers.append("QNNExecutionProvider")
            active_target = "Qualcomm NPU (QNN)"
        elif "OpenVINOExecutionProvider" in avail:
            providers.append(("OpenVINOExecutionProvider", {"device_type": "NPU"}))
            active_target = "Intel NPU (OpenVINO)"

    if npu_only:
        if not providers:
            print("[NPU_ONLY] ERROR: No NPU ExecutionProvider available! Available:", avail)
            return [], "NPU_UNAVAILABLE"
        print(f"[NPU_ONLY] Strict NPU-only providers active: {providers} ({active_target})")
        return providers, active_target

    if use_gpu and not (use_npu and "VitisAIExecutionProvider" in avail):
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


def get_providers_for_profile(available_providers, profile):
    """Return (obj_providers, face_providers, label) for explicit testing/runtime profiles."""
    avail = list(available_providers or [])
    amd_info = detect_amd_ryzen_ai()
    if amd_info.get("install_dir") and not os.environ.get("RYZEN_AI_INSTALLATION_PATH"):
        os.environ["RYZEN_AI_INSTALLATION_PATH"] = amd_info["install_dir"]

    has_npu = "VitisAIExecutionProvider" in avail
    has_gpu = "DmlExecutionProvider" in avail or "CUDAExecutionProvider" in avail
    gpu_ep = "CUDAExecutionProvider" if "CUDAExecutionProvider" in avail else ("DmlExecutionProvider" if "DmlExecutionProvider" in avail else None)

    if profile == "only_cpu":
        return ["CPUExecutionProvider"], ["CPUExecutionProvider"], "Only CPU"
    if profile == "only_gpu":
        if not gpu_ep:
            return [], [], "GPU Unavailable"
        return [gpu_ep], [gpu_ep], f"Only GPU ({gpu_ep})"
    if profile == "only_npu":
        if not has_npu:
            return [], [], "NPU Unavailable"
        return ["VitisAIExecutionProvider"], ["VitisAIExecutionProvider"], "Only NPU (Strict VitisAI)"
    if profile == "npu_cpu":
        p = (["VitisAIExecutionProvider"] if has_npu else []) + ["CPUExecutionProvider"]
        return p, list(p), "NPU + CPU"
    if profile == "npu_gpu":
        p = (["VitisAIExecutionProvider"] if has_npu else []) + ([gpu_ep] if gpu_ep else [])
        return p or ["CPUExecutionProvider"], p or ["CPUExecutionProvider"], "NPU + GPU"
    if profile == "hybrid_90_10":
        obj_p = (["VitisAIExecutionProvider"] if has_npu else []) + ["CPUExecutionProvider"]
        face_p = ([gpu_ep] if gpu_ep else []) + ["CPUExecutionProvider"]
        return obj_p, face_p, "Hybrid 90:10 (NPU Obj + GPU Face)"
    # 'all'
    p = (["VitisAIExecutionProvider"] if has_npu else []) + ([gpu_ep] if gpu_ep else []) + ["CPUExecutionProvider"]
    return p, list(p), "All (NPU + GPU + CPU)"


def get_accelerator_summary(available_providers):
    """Return human-readable accelerator status for diagnostics & UI."""
    providers, active_target = get_execution_providers(available_providers)
    return {
        "active_target": active_target,
        "available_providers": list(available_providers or []),
        "configured_providers": [p[0] if isinstance(p, tuple) else p for p in providers],
        "amd_ryzen_ai": detect_amd_ryzen_ai(),
    }
