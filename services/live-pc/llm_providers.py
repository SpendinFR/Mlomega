from __future__ import annotations

"""LLMProvider registry — local (Ollama) + real cloud (OpenAI / Gemini) — E33 §3.

The live pipeline needs a single, swappable text-LLM surface for three callers:
the IntentRouter's parse fallback, ``memory_query``'s honest-degraded wording, and
the optional "c'est quoi ça" deep answer. Each provider exposes one method::

    complete_json(system, user, schema_hint=None, timeout=None) -> dict

which returns strict JSON (parsed dict) or raises :class:`LLMUnavailable` — never a
half-parsed string, never a silent stub. A ``complete_text`` convenience wraps it.

* :class:`OllamaProvider` — reuses the core ``OllamaJsonClient`` when importable
  (one JSON contract across the whole system), else a thin urllib call to
  ``/api/generate``; unreachable Ollama degrades honestly.
* :class:`OpenAIProvider` — real HTTP to ``/chat/completions`` with
  ``response_format={"type":"json_object"}``; key from ``OPENAI_API_KEY`` (env) or
  the profile.
* :class:`GeminiProvider` — real HTTP to ``…/models/<model>:generateContent`` with
  ``responseMimeType=application/json``; key from ``GEMINI_API_KEY``.

Runtime switch (``LLMRouter``): "mode payant [openai|gemini]" / "mode local".
The switch is refused (politely) when the profile's ``cloud_data_policy`` is
``local_only`` — cloud is NEVER active by default and NEVER without an explicit,
policy-permitted opt-in. The switch reply carries the estimated cost range and a
``cloud_active`` StatusBar event is emitted to the device.

Endpoints/models/costs come from ``configs/cloud_llm.yaml`` (configurable — see
DECISIONS §E33). HTTP uses ``requests`` if present, else stdlib ``urllib``.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class LLMUnavailable(RuntimeError):
    """Raised when a provider cannot answer (unreachable / no key / bad reply)."""


# --------------------------------------------------------------------------- config
_DEFAULT_CONFIG: dict[str, Any] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.4-mini",
        "api_key_env": "OPENAI_API_KEY",
        "cost_eur_per_question": [0.01, 0.03],
        "timeout_s": 30,
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "cost_eur_per_question": [0.005, 0.02],
        "timeout_s": 30,
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "model": None,
        "cost_eur_per_question": [0.0, 0.0],
        "timeout_s": 60,
    },
}


def load_cloud_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load ``configs/cloud_llm.yaml`` (endpoints/models/costs). Falls back to
    baked defaults so a bare checkout still routes locally."""
    p = Path(path) if path else _ROOT / "configs" / "cloud_llm.yaml"
    cfg = {k: dict(v) for k, v in _DEFAULT_CONFIG.items()}
    if p.exists():
        try:
            import yaml

            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for provider, values in raw.items():
                if isinstance(values, dict):
                    cfg.setdefault(provider, {}).update(values)
        except Exception:
            pass
    return cfg


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST JSON and return parsed JSON. Uses requests if available, else urllib.
    Raises :class:`LLMUnavailable` on any transport/decode error."""
    body = json.dumps(payload).encode("utf-8")
    try:
        import requests  # type: ignore

        resp = requests.post(url, data=body, headers={"Content-Type": "application/json", **headers}, timeout=timeout)
        if resp.status_code >= 400:
            raise LLMUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    except ImportError:
        pass
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise LLMUnavailable(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LLMUnavailable(str(exc)[:200]) from exc


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model reply, tolerating code fences / prose."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(s[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    raise LLMUnavailable("no JSON object in reply")


# --------------------------------------------------------------------------- providers
class LLMProvider:
    """Text-LLM interface. Cloud providers set ``is_cloud=True``."""

    name = "base"
    is_cloud = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.model = self.config.get("model")
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        self.timeout_s = float(self.config.get("timeout_s") or 30)

    def complete_json(self, system: str, user: str, *, schema_hint: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def complete_text(self, prompt: str, *, timeout: float | None = None) -> str:
        out = self.complete_json(
            "Réponds en JSON strict: {\"text\": \"...\"}.",
            prompt,
            schema_hint={"text": "string"},
            timeout=timeout,
        )
        return str(out.get("text") or "").strip()

    def cost_range(self) -> tuple[float, float]:
        c = self.config.get("cost_eur_per_question") or [0.0, 0.0]
        try:
            return float(c[0]), float(c[1])
        except Exception:
            return 0.0, 0.0

    def available(self) -> bool:
        return True


class OllamaProvider(LLMProvider):
    name = "ollama"
    is_cloud = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or _DEFAULT_CONFIG["ollama"])
        self.base_url = self.base_url or "http://127.0.0.1:11434"

    def _core_client(self) -> Any | None:
        try:
            from mlomega_audio_elite.llm import OllamaJsonClient  # type: ignore
        except Exception:
            return None
        try:
            return OllamaJsonClient(base_url=self.base_url, model=self.model)
        except Exception:
            return None

    def complete_json(self, system: str, user: str, *, schema_hint: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        t = float(timeout or self.timeout_s)
        client = self._core_client()
        if client is not None:
            try:
                data = client.require_json(system, user, schema_hint=schema_hint, timeout=t)
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                raise LLMUnavailable(str(exc)[:200]) from exc
        # Fallback: raw /api/generate call.
        prompt = f"{system}\n\n{user}\n\nRéponds uniquement en JSON valide."
        payload = {"model": self.model or "qwen2.5", "prompt": prompt, "stream": False, "format": "json"}
        data = _http_post_json(self.base_url + "/api/generate", payload, {}, t)
        return _extract_json(str(data.get("response") or ""))

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags", timeout=2) as r:  # noqa: S310
                tags = json.loads(r.read().decode("utf-8"))
            return bool(tags.get("models"))
        except Exception:
            return False


class OpenAIProvider(LLMProvider):
    name = "openai"
    is_cloud = True

    def __init__(self, config: dict[str, Any] | None = None, *, api_key: str | None = None) -> None:
        super().__init__(config or _DEFAULT_CONFIG["openai"])
        self.base_url = self.base_url or "https://api.openai.com/v1"
        self.model = self.model or "gpt-5.4-mini"
        env = str(self.config.get("api_key_env") or "OPENAI_API_KEY")
        self.api_key = api_key or os.environ.get(env) or ""

    def available(self) -> bool:
        return bool(self.api_key)

    def complete_json(self, system: str, user: str, *, schema_hint: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise LLMUnavailable("no OPENAI_API_KEY")
        sys_msg = system
        if schema_hint:
            sys_msg = f"{system}\nSchéma JSON: {json.dumps(schema_hint, ensure_ascii=False)}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        data = _http_post_json(
            self.base_url + "/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            float(timeout or self.timeout_s),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LLMUnavailable(f"unexpected OpenAI reply: {str(data)[:150]}") from exc
        return _extract_json(content)


class GeminiProvider(LLMProvider):
    name = "gemini"
    is_cloud = True

    def __init__(self, config: dict[str, Any] | None = None, *, api_key: str | None = None) -> None:
        super().__init__(config or _DEFAULT_CONFIG["gemini"])
        self.base_url = self.base_url or "https://generativelanguage.googleapis.com/v1beta"
        self.model = self.model or "gemini-2.5-flash"
        env = str(self.config.get("api_key_env") or "GEMINI_API_KEY")
        self.api_key = api_key or os.environ.get(env) or ""

    def available(self) -> bool:
        return bool(self.api_key)

    def complete_json(self, system: str, user: str, *, schema_hint: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise LLMUnavailable("no GEMINI_API_KEY")
        prompt = system
        if schema_hint:
            prompt += f"\nSchéma JSON: {json.dumps(schema_hint, ensure_ascii=False)}"
        prompt += f"\n\n{user}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        data = _http_post_json(url, payload, {}, float(timeout or self.timeout_s))
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            raise LLMUnavailable(f"unexpected Gemini reply: {str(data)[:150]}") from exc
        return _extract_json(text)


_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "ollama": OllamaProvider,
    "local": OllamaProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


# --------------------------------------------------------------------------- router
class LLMRouter:
    """Owns the active provider and the runtime local<->cloud switch (E33 §3).

    The switch respects the profile's ``cloud_data_policy``: ``local_only`` refuses
    cloud with a polite reply; otherwise a cloud provider is activated only with a
    key present. A ``cloud_active`` event is emitted to ``on_cloud_event`` (the
    pipeline wires it to the device StatusBar).
    """

    def __init__(
        self,
        *,
        profile: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        on_cloud_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.profile = profile or {}
        self.config = config or load_cloud_config()
        self.on_cloud_event = on_cloud_event
        self.cloud_data_policy = str(self.profile.get("cloud_data_policy") or "local_only")
        # Start LOCAL, always (cloud is opt-in, never default).
        self.active: LLMProvider = OllamaProvider(self.config.get("ollama"))
        self.metrics: dict[str, Any] = {"cloud_switches": 0, "local_switches": 0, "cloud_refused": 0}

    @property
    def cloud_active(self) -> bool:
        return self.active.is_cloud

    @property
    def mode(self) -> str:
        return self.active.name if self.active.is_cloud else "local"

    def _emit_cloud_event(self) -> None:
        if self.on_cloud_event is None:
            return
        low, high = self.active.cost_range()
        try:
            self.on_cloud_event({
                "type": "status",
                "kind": "cloud_mode",
                "cloud_active": self.cloud_active,
                "provider": self.active.name,
                "cost_eur_per_question": [low, high],
            })
        except Exception:
            pass

    def switch_to_cloud(self, provider: str = "openai", *, api_key: str | None = None) -> dict[str, Any]:
        """Activate a cloud provider (opt-in). Refused under ``local_only``."""
        provider = (provider or "openai").lower()
        if provider not in ("openai", "gemini"):
            provider = "openai"
        if self.cloud_data_policy == "local_only":
            self.metrics["cloud_refused"] += 1
            return {
                "ok": False,
                "cloud_active": self.cloud_active,
                "reason": "local_only",
                "text": "Mode payant refusé : ta politique de données est « local uniquement ». "
                        "Change-la dans le profil pour autoriser le cloud.",
            }
        cls = _PROVIDER_CLASSES[provider]
        prov = cls(self.config.get(provider), api_key=api_key)  # type: ignore[call-arg]
        if not prov.available():
            return {
                "ok": False,
                "cloud_active": self.cloud_active,
                "reason": "no_api_key",
                "text": f"Mode payant impossible : aucune clé {provider.upper()} configurée.",
            }
        self.active = prov
        self.metrics["cloud_switches"] += 1
        low, high = prov.cost_range()
        self._emit_cloud_event()
        return {
            "ok": True,
            "cloud_active": True,
            "provider": provider,
            "model": prov.model,
            "cost_eur_per_question": [low, high],
            "text": f"Mode payant activé ({provider}) — ~{low:.2f}–{high:.2f} €/question.",
        }

    def switch_to_local(self) -> dict[str, Any]:
        self.active = OllamaProvider(self.config.get("ollama"))
        self.metrics["local_switches"] += 1
        self._emit_cloud_event()
        return {"ok": True, "cloud_active": False, "provider": "ollama", "text": "Mode local activé (gratuit)."}

    # convenience delegation -------------------------------------------------
    def complete_json(self, system: str, user: str, *, schema_hint: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        return self.active.complete_json(system, user, schema_hint=schema_hint, timeout=timeout)

    def available(self) -> bool:
        return self.active.available()
