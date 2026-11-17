"""Hardware Accelerator & NPU Support Package (CPU base -> GPU -> NPU when available)."""

from .accelerator_manager import get_execution_providers, get_accelerator_summary
from .amd_xdna2 import detect_amd_ryzen_ai

__all__ = ["get_execution_providers", "get_accelerator_summary", "detect_amd_ryzen_ai"]
