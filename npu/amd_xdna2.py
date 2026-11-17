"""AMD Ryzen AI (XDNA / XDNA 2) NPU detection and configuration helper."""

import os
import glob


def detect_amd_ryzen_ai():
    """Return AMD Ryzen AI SDK paths if installed on this machine."""
    install_dir = os.environ.get("RYZEN_AI_INSTALLATION_PATH", "").strip()
    if not install_dir or not os.path.isdir(install_dir):
        candidates = sorted(glob.glob(r"C:\Program Files\RyzenAI\*"), reverse=True)
        install_dir = next((c for c in candidates if os.path.isdir(c) and os.path.isdir(os.path.join(c, "voe-4.0-win_amd64"))), "")

    vaip_cfg = os.environ.get("VAIP_CONFIG") or os.environ.get("RYZEN_AI_VAIP_CONFIG") or ""
    if not vaip_cfg and install_dir:
        for pat in (
            os.path.join(install_dir, "voe-4.0-win_amd64", "vaip_config.json"),
            os.path.join(install_dir, "**", "vaip_config.json"),
        ):
            hits = glob.glob(pat, recursive=True)
            if hits:
                vaip_cfg = hits[0]
                break

    xclbin = os.environ.get("XLNX_VART_FIRMWARE", "")
    if not xclbin and install_dir:
        hits = glob.glob(os.path.join(install_dir, "**", "*.xclbin"), recursive=True)
        if hits:
            xclbin = hits[0]

    return {
        "sdk_installed": bool(install_dir and os.path.isdir(install_dir)),
        "install_dir": install_dir or None,
        "vaip_config": vaip_cfg or None,
        "xclbin": xclbin or None,
        "target_arch": "AMD XDNA / XDNA 2 NPU (VitisAI EP)",
    }
