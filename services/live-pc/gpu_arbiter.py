from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

JobClass = Literal['tracker','detector','ocr','vlm','deep']
_PRIORITY = {'tracker':100,'detector':80,'ocr':50,'vlm':30,'deep':10}

@dataclass
class GpuSnapshot:
    total_mb: int = 0
    used_mb: int = 0
    available: bool = False

class GpuArbiter:
    def __init__(self, *, max_used_ratio: float = 0.90) -> None:
        self.max_used_ratio = max_used_ratio
        self.degraded_reasons: list[str] = []
    def snapshot(self) -> GpuSnapshot:
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit(); h = pynvml.nvmlDeviceGetHandleByIndex(0); mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            return GpuSnapshot(total_mb=int(mem.total/1048576), used_mb=int(mem.used/1048576), available=True)
        except Exception:
            return GpuSnapshot()
    def request(self, job_class: JobClass) -> dict[str, object]:
        snap = self.snapshot()
        if not snap.available:
            return {'grant': job_class in {'tracker'}, 'reason': 'gpu_unavailable', 'snapshot': snap}
        ratio = snap.used_mb / max(1, snap.total_mb)
        if ratio >= self.max_used_ratio and _PRIORITY[job_class] < _PRIORITY['detector']:
            self.degraded_reasons.append('gpu_vram_pressure')
            return {'grant': False, 'reason': 'gpu_vram_pressure', 'snapshot': snap}
        return {'grant': True, 'reason': 'ok', 'snapshot': snap}
