"""User capability profile loader (handoff §3.5).

`configs/user_profile.yaml` declares which adapters the session uses
(display/capture/llm/vision/asr/cloud policy). Written by
`scripts/setup_profile.ps1`; read here by the live services so that changing a
value never requires a code change. Values map 1:1 to adapter implementations —
an invalid value falls back to the safe default with a warning rather than
crashing a live session.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

_DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "configs" / "user_profile.yaml"

# Allowed values per handoff §3.5. First entry = safe default.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "display": ("companion_web", "phone_only", "xreal_one_pro", "spectacles"),
    "capture": ("phone_camera", "xreal_eye", "none"),
    "llm": ("ollama_local", "openai", "gemini", "anthropic"),
    "vision": ("onnx_local", "cloud"),
    "asr": ("local", "cloud"),
    "cloud_data_policy": ("local_only", "allow_crops", "allow_transcripts"),
}


def load_user_profile(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate the user profile; unknown/invalid values fall back."""
    profile_path = Path(path) if path else _DEFAULT_PROFILE_PATH
    raw: dict[str, Any] = {}
    if profile_path.exists():
        try:
            import yaml

            data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except Exception as exc:
            warnings.warn(f"user_profile.yaml unreadable ({exc}); using defaults", RuntimeWarning)

    profile: dict[str, Any] = {}
    for key, allowed in _ALLOWED.items():
        value = str(raw.get(key) or allowed[0])
        if value not in allowed:
            warnings.warn(
                f"user_profile.{key}={value!r} is not one of {allowed}; falling back to {allowed[0]!r}",
                RuntimeWarning,
            )
            value = allowed[0]
        profile[key] = value
    # Pass through free-form extras (llm_model, ports…) untouched.
    for key, value in raw.items():
        if key not in profile:
            profile[key] = value
    return profile


def renderer_route(profile: dict[str, Any]) -> str:
    """Which delivery route renders the UI for this profile.

    phone_only / companion_web render through the delivery-adapter WebSocket
    (the phone/browser viewer); XR glasses render through the WebRTC
    DataChannel to the Unity runtime.
    """
    display = str(profile.get("display") or "companion_web")
    return "websocket" if display in ("phone_only", "companion_web") else "datachannel"
