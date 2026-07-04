"""GpuArbiter — NVML VRAM accounting + per-job-class budgets + Ollama unload verification.

Handoff §4.1: live workloads must stay under a strict VRAM budget on an 8 Go
RTX 3070. Priorities (guide §10.4) degrade in the order detector -> floor,
pause changes, refuse VLM, never touch tracker/subtitles.

Two capabilities were missing and are added here:

* ``request(job_class)`` now honours a per-class VRAM budget read from
  ``configs/profiles/rtx3070.yaml`` (piège #? — budgets must live in config,
  never hard-coded in the business logic). A grant is refused when admitting the
  job would push resident usage past its class budget.
* ``verify_ollama_unload(model, base_url)`` re-measures VRAM *and* queries the
  Ollama ``/api/ps`` endpoint after an unload; it returns ``False`` with a
  warning when the model is still resident (piège #10 — an ``ollama_unload`` can
  fail silently and saturate the 8 Go).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

JobClass = Literal["tracker", "detector", "asr", "ocr", "vlm", "deep", "live_llm"]

# Guide §10.4 priority order (higher = protected first). Never preempt tracker.
# ASR (faster-whisper subtitles) sits just under the detector: subtitles are a
# reflex path (handoff §3.6 "never touch the tracker or subtitles") so ASR is
# protected above the on-demand OCR/VLM/LLM classes.
_PRIORITY: dict[str, int] = {
    "tracker": 100,
    "detector": 80,
    "asr": 70,
    "ocr": 50,
    "live_llm": 40,
    "vlm": 30,
    "deep": 10,
}

# Default profile location relative to the repo root (services/live-pc/..).
_DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "configs" / "profiles" / "rtx3070.yaml"

# Fallback budgets in MB, derived from handoff §4.1 (live day <= 5.5 Go total).
# Used only when the profile file lacks a gpu.job_budgets_mb section.
_FALLBACK_BUDGETS_MB: dict[str, int] = {
    "tracker": 512,      # CPU-side ByteTrack, tiny GPU footprint
    "detector": 512,     # YOLO-nano FP16 ~0.3 Go
    "asr": 1024,         # faster-whisper small int8 ~1 Go
    "ocr": 768,          # OCR/embeddings on demand ~0.7 Go
    "live_llm": 3072,    # live 4B q4 ~2.5-3 Go
    "vlm": 5632,         # targeted VLM crop, one job at a time (5.5 Go)
    "deep": 8192,        # night phase, live off
}


def _load_gpu_config(profile_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(profile_path) if profile_path else _DEFAULT_PROFILE
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        gpu = data.get("gpu") if isinstance(data, dict) else None
        return gpu if isinstance(gpu, dict) else {}
    except Exception:  # pragma: no cover - malformed profile falls back to defaults
        return {}


@dataclass
class GpuSnapshot:
    total_mb: int = 0
    used_mb: int = 0
    available: bool = False

    @property
    def free_mb(self) -> int:
        return max(0, self.total_mb - self.used_mb)


class GpuArbiter:
    def __init__(
        self,
        *,
        max_used_ratio: float = 0.90,
        profile_path: Path | str | None = None,
        job_budgets_mb: dict[str, int] | None = None,
    ) -> None:
        self.max_used_ratio = max_used_ratio
        self.degraded_reasons: list[str] = []
        gpu_cfg = _load_gpu_config(profile_path)
        # Ratio may be overridden by the profile (max_live_vram_ratio).
        cfg_ratio = gpu_cfg.get("max_live_vram_ratio")
        if isinstance(cfg_ratio, (int, float)):
            self.max_used_ratio = float(cfg_ratio)
        budgets = dict(_FALLBACK_BUDGETS_MB)
        cfg_budgets = gpu_cfg.get("job_budgets_mb")
        if isinstance(cfg_budgets, dict):
            for key, value in cfg_budgets.items():
                try:
                    budgets[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue
        if job_budgets_mb:
            budgets.update({str(k): int(v) for k, v in job_budgets_mb.items()})
        self.job_budgets_mb = budgets

    # ------------------------------------------------------------------ NVML
    def snapshot(self) -> GpuSnapshot:
        try:
            import pynvml  # type: ignore

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return GpuSnapshot(
                total_mb=int(mem.total / 1048576),
                used_mb=int(mem.used / 1048576),
                available=True,
            )
        except Exception:
            return GpuSnapshot()

    # ------------------------------------------------------------- admission
    def request(self, job_class: JobClass) -> dict[str, object]:
        snap = self.snapshot()
        if job_class not in _PRIORITY:
            return {"grant": False, "reason": "unknown_job_class", "snapshot": snap}
        if not snap.available:
            # Without NVML we can only safely admit the CPU-bound tracker.
            grant = job_class in {"tracker"}
            return {"grant": grant, "reason": "gpu_unavailable", "snapshot": snap}

        ratio = snap.used_mb / max(1, snap.total_mb)
        # Protected floor: tracker/detector/asr are the reflex classes handoff
        # §3.6 says must never be starved ("never touch the tracker or
        # subtitles"). Everything strictly below asr priority is on-demand and
        # can be pressure/budget-denied.
        protected_floor = _PRIORITY["asr"]
        # Global pressure: refuse on-demand classes only.
        if ratio >= self.max_used_ratio and _PRIORITY[job_class] < protected_floor:
            self.degraded_reasons.append("gpu_vram_pressure")
            return {"grant": False, "reason": "gpu_vram_pressure", "snapshot": snap}

        # Per-class budget: applies only to on-demand classes (below the
        # protected floor). Resident reflex classes (tracker/detector/asr,
        # handoff §4.1 priority 1) are never budget-denied.
        budget = self.job_budgets_mb.get(job_class)
        if budget is not None and snap.used_mb > budget and _PRIORITY[job_class] < protected_floor:
            self.degraded_reasons.append(f"budget_exceeded:{job_class}")
            return {
                "grant": False,
                "reason": "job_budget_exceeded",
                "job_class": job_class,
                "budget_mb": budget,
                "used_mb": snap.used_mb,
                "snapshot": snap,
            }
        return {"grant": True, "reason": "ok", "job_class": job_class, "budget_mb": budget, "snapshot": snap}

    # ----------------------------------------------------- ollama verification
    def _ollama_ps(self, base_url: str, timeout: float = 8.0) -> list[dict[str, Any]]:
        url = base_url.rstrip("/") + "/api/ps"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local trusted URL
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return []
        models = payload.get("models") if isinstance(payload, dict) else None
        return models if isinstance(models, list) else []

    def verify_ollama_unload(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        *,
        before_used_mb: int | None = None,
    ) -> dict[str, object]:
        """Confirm ``model`` is no longer resident after an ``ollama_unload``.

        Piège #10: never trust a silent unload. We query ``/api/ps`` and
        re-measure VRAM. Returns ``{"unloaded": bool, ...}`` and emits a warning
        when the model is still loaded.
        """
        snap = self.snapshot()
        resident = self._ollama_ps(base_url)
        still_loaded = any(
            str(entry.get("name") or entry.get("model") or "").split(":")[0] == model.split(":")[0]
            or str(entry.get("name") or entry.get("model") or "") == model
            for entry in resident
        )
        freed_mb = None
        if before_used_mb is not None and snap.available:
            freed_mb = before_used_mb - snap.used_mb
        result: dict[str, object] = {
            "unloaded": not still_loaded,
            "model": model,
            "still_resident": still_loaded,
            "resident_models": [entry.get("name") or entry.get("model") for entry in resident],
            "freed_mb": freed_mb,
            "snapshot": snap,
        }
        if still_loaded:
            warnings.warn(
                f"ollama_unload verification FAILED: '{model}' still resident after unload "
                f"(/api/ps reports it loaded). VRAM may saturate.",
                RuntimeWarning,
                stacklevel=2,
            )
        return result
