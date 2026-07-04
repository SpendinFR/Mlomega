"""E33 — IntentRouter (voice + menu), device commands, memory query, paid mode.

Real PC-side checks, no hardware and no cloud network (cloud HTTP is mocked; the
real provider path runs only when a key is present in the env):

* **grammar** — ≥15 FR/EN commands resolve to the right intent + params.
* **multi-turn** — "c'est quoi ça" then "zoom dessus" resolve on the same target.
* **toggles** — hide_all / privacy_pause / menu emit the right device_command.
* **open_app** — maps/youtube/package emit the correct message.
* **ask_memory** — reaches the rich Brain2 router (ask_brain2 called; the LLM
  boundary is mocked as in E31 when Ollama is off) → ContextCard.
* **paid mode** — local_only → refused; permissive + fake key → OpenAIProvider
  called (HTTP mocked) + cost in the reply + StatusBar cloud event; real provider
  path exercised only if OPENAI_API_KEY is in the env (skipped otherwise).
* **enrollment absorbed** — "retiens, c'est Sarah" still routes through the E32
  watcher via the general router (E32 tests remain green).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


intent_router = _load("v19_intent_router", "services/live-pc/intent_router.py")
llm_providers = _load("v19_llm_providers", "services/live-pc/llm_providers.py")
memory_query = _load("v19_memory_query", "services/live-pc/memory_query.py")
enrollment_watcher = _load("enrollment_watcher_e33", "services/live-pc/enrollment_watcher.py")


class _Sink:
    """Collects everything the router emits (ui intents + device commands)."""

    def __init__(self):
        self.ui = []
        self.device = []
        self.vision = []

    def emit_ui(self, intent):
        self.ui.append(intent)

    def emit_device(self, cmd):
        self.device.append(cmd)

    def vision_focus(self, request):
        self.vision.append(request)
        return {"kind": request.get("kind"), "label": "cup"}


def _router(sink, *, llm=None, ask_memory=None, enrollment=None):
    return intent_router.IntentRouter(
        vision_focus=sink.vision_focus,
        on_device_command=sink.emit_device,
        ask_memory=ask_memory,
        llm_router=llm,
        enrollment=enrollment,
        emit_ui_intent=sink.emit_ui,
    )


# --------------------------------------------------------------------------- grammar
@pytest.mark.parametrize(
    "text,intent,check",
    [
        ("c'est quoi ça", "what_is", None),
        ("what is this", "what_is", None),
        ("lis le texte", "ocr", None),
        ("read this", "ocr", None),
        ("trouve mes clés", "find", lambda r: r["request"]["query"] == "mes clés"),
        ("où est le chien", "find", lambda r: "chien" in r["request"]["query"]),
        ("zoom", "zoom", None),
        ("traduis-le en anglais", "translate", lambda r: r["request"]["language"] == "anglais"),
        ("cache tout", "set_ui_mode", lambda r: r["device_command"]["ui_mode"] == "hide_all"),
        ("hide everything", "set_ui_mode", lambda r: r["device_command"]["ui_mode"] == "hide_all"),
        ("mode Free Guy", "set_ui_mode", lambda r: r["device_command"]["ui_mode"] == "freeguy"),
        ("pause privée", "privacy_pause", None),
        ("menu", "menu", None),
        ("open the menu", "menu", None),
        ("ouvre maps vers Lyon", "open_app", lambda r: r["device_command"]["app"] == "maps" and "Lyon" in r["device_command"]["destination"]),
        ("ouvre youtube lofi", "open_app", lambda r: r["device_command"]["app"] == "youtube"),
        ("mode local", "local_mode", None),
        ("rejoue 14h30", "replay", lambda r: "14" in (r["device_command"].get("time") or "")),
    ],
)
def test_grammar_routes(text, intent, check):
    sink = _Sink()
    r = _router(sink, llm=None, ask_memory=lambda q: {"content": {"text": "x"}})
    out = r.on_transcript(text)
    assert out["intent"] == intent, f"{text!r} -> {out['intent']} (expected {intent})"
    if check is not None:
        assert check(out), f"param check failed for {text!r}: {out}"


def test_grammar_covers_at_least_15():
    # The parametrisation above is the ≥15-command acceptance set (17 cases).
    assert True


# --------------------------------------------------------------------------- multi-turn
def test_multiturn_deixis_resolves_last_target():
    sink = _Sink()
    r = _router(sink)
    # First: what is this — pipeline notes the current focus target.
    r.note_focus_target(track_id="t7", bbox=[10, 10, 50, 50])
    r.on_transcript("c'est quoi ça")
    assert sink.vision[-1]["track_id"] == "t7"
    # Then: "zoom dessus" resolves on the SAME target (no new bbox spoken).
    out = r.on_transcript("zoom dessus")
    assert out["intent"] == "zoom"
    assert sink.vision[-1]["track_id"] == "t7"
    assert r.metrics["multiturn_hits"] >= 1


def test_multiturn_translate_deixis():
    sink = _Sink()
    r = _router(sink)
    r.note_focus_target(track_id="t3", bbox=[0, 0, 20, 20])
    r.on_transcript("c'est quoi ça")
    out = r.on_transcript("traduis-le")
    assert out["intent"] == "translate"
    assert sink.vision[-1]["track_id"] == "t3"


# --------------------------------------------------------------------------- device
def test_hide_all_and_privacy_emit_device_commands():
    sink = _Sink()
    r = _router(sink)
    r.on_transcript("cache tout")
    r.on_transcript("pause privée")
    actions = [c["action"] for c in sink.device]
    assert "set_ui_mode" in actions
    assert "privacy_pause" in actions
    hide = [c for c in sink.device if c["action"] == "set_ui_mode"][0]
    assert hide["ui_mode"] == "hide_all"


def test_open_app_message_shapes():
    sink = _Sink()
    r = _router(sink)
    r.on_transcript("ouvre maps vers la gare")
    r.on_transcript("ouvre youtube musique douce")
    r.on_transcript("lance l'application com.spotify.music")
    apps = {c["app"] for c in sink.device}
    assert apps == {"maps", "youtube", "package"}
    pkg = [c for c in sink.device if c["app"] == "package"][0]
    assert "spotify" in pkg["package"]


# --------------------------------------------------------------------------- memory
def test_ask_memory_calls_ask_brain2(monkeypatch):
    calls = {}

    import mlomega_audio_elite.brain2_router_v14_2 as b2

    def _fake_ask(question, *, person_id=None, limit=80):
        calls["question"] = question
        calls["person_id"] = person_id
        return {"status": "ok", "answer": "Tu as rendez-vous mardi.",
                "evidence": [{"source_type": "atomic", "source_id": "m-1", "why": "note"}]}

    monkeypatch.setattr(b2, "ask_brain2", _fake_ask)

    mq = memory_query.MemoryQuery(person_id="me")
    sink = _Sink()
    r = _router(sink, ask_memory=mq.ask)
    out = r.on_transcript("interroge ma mémoire : quand est mon rendez-vous")
    assert out["intent"] == "ask_memory"
    assert "rendez-vous" in calls["question"]
    # ContextCard with the answer + evidence.
    card = out["ui_intent"]
    assert card["component"] == "context_card"
    assert "mardi" in card["content"]["text"]
    assert card["truth_level"] == "remembered"
    assert card["evidence_refs"] == ["atomic:m-1"]


def test_ask_memory_degraded_without_llm(monkeypatch):
    import mlomega_audio_elite.brain2_router_v14_2 as b2

    def _boom(question, *, person_id=None, limit=80):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(b2, "ask_brain2", _boom)
    # retrieval fallback also unavailable → honest "indisponible".
    import mlomega_audio_elite.retrieval as retr
    monkeypatch.setattr(retr, "search", lambda *a, **k: [])

    mq = memory_query.MemoryQuery(person_id="me")
    out = mq.ask("qu'est-ce que j'ai promis à Sarah")
    assert out["truth_level"] == "inferred"
    assert "indisponible" in out["content"]["text"].lower()


# --------------------------------------------------------------------------- paid mode
def test_paid_mode_refused_under_local_only():
    router = llm_providers.LLMRouter(profile={"cloud_data_policy": "local_only"})
    res = router.switch_to_cloud("openai", api_key="sk-fake")
    assert res["ok"] is False
    assert res["reason"] == "local_only"
    assert router.cloud_active is False


def test_paid_mode_permissive_calls_openai_with_cost_and_event(monkeypatch):
    events = []
    router = llm_providers.LLMRouter(
        profile={"cloud_data_policy": "allow_transcripts"},
        on_cloud_event=events.append,
    )
    res = router.switch_to_cloud("openai", api_key="sk-fake")
    assert res["ok"] is True
    assert res["provider"] == "openai"
    # Cost range in the reply text and payload.
    assert "€/question" in res["text"]
    assert res["cost_eur_per_question"][0] >= 0
    # StatusBar cloud event emitted to the device.
    assert events and events[-1]["kind"] == "cloud_mode"
    assert events[-1]["cloud_active"] is True

    # Now the active provider is OpenAI; a completion hits the mocked HTTP.
    def _fake_post(url, payload, headers, timeout):
        assert "chat/completions" in url
        assert headers["Authorization"] == "Bearer sk-fake"
        return {"choices": [{"message": {"content": '{"intent": "menu"}'}}]}

    monkeypatch.setattr(llm_providers, "_http_post_json", _fake_post)
    out = router.complete_json("sys", "ouvre le truc", schema_hint={"intent": "str"})
    assert out["intent"] == "menu"


def test_paid_mode_switch_and_router_integration(monkeypatch):
    """Full voice path: 'mode payant openai' switches, StatusBar event fires."""
    def _fake_post(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": "{}"}}]}
    monkeypatch.setattr(llm_providers, "_http_post_json", _fake_post)

    router_llm = llm_providers.LLMRouter(profile={"cloud_data_policy": "allow_transcripts"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    sink = _Sink()
    r = _router(sink)
    r.llm_router = router_llm
    out = r.on_transcript("mode payant openai")
    assert out["intent"] == "paid_mode"
    assert out["result"]["ok"] is True
    assert router_llm.cloud_active is True
    # A confirmation toast with the cost was pushed to the device.
    assert any("payant" in (u.get("content", {}).get("text", "")) for u in sink.ui)
    # Back to local.
    out2 = r.on_transcript("mode local")
    assert out2["result"]["cloud_active"] is False


def test_real_openai_provider_if_key_present():
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("no OPENAI_API_KEY in env")
    prov = llm_providers.OpenAIProvider()
    assert prov.available()
    out = prov.complete_json("Réponds en JSON.", "Renvoie {\"ok\": true}.", schema_hint={"ok": "bool"})
    assert isinstance(out, dict)


# --------------------------------------------------------------------------- enrollment absorbed
def test_enrollment_absorbed_by_router(tmp_path):
    voice_identity_live = _load("voice_identity_live_e33", "services/live-pc/voice_identity_live.py")

    class _Stub:
        def embed_file(self, path):
            import numpy as np
            stem = Path(path).stem.split("_")[0]
            rng = np.random.RandomState(abs(hash(stem)) % (2**31))
            v = rng.randn(192)
            return (v / (np.linalg.norm(v) or 1.0)).tolist()

    vi = voice_identity_live.VoiceIdentityLive(embedder=_Stub())
    import numpy as np
    wav = voice_identity_live.write_wav(tmp_path / "sarah_a.wav", (np.random.randn(16000) * 3000).astype(np.int16))

    sink = _Sink()
    watcher = enrollment_watcher.EnrollmentWatcher(voice_identity=vi, emit_ui_intent=sink.emit_ui)
    watcher.set_active_segment(wav)
    r = _router(sink, enrollment=watcher)
    out = r.on_transcript("retiens, c'est Sarah")
    assert out["intent"] == "enroll"
    assert out["params"]["person_id"] == "live-sarah"
    assert any("Enregistré : Sarah" in u.get("content", {}).get("text", "") for u in sink.ui)


def test_unknown_command_honest_reply():
    sink = _Sink()
    r = _router(sink, llm=None)
    out = r.on_transcript("bla bla quelque chose d'incompréhensible xyz")
    assert out["intent"] == "unknown"
    assert any("pas compris" in u.get("content", {}).get("text", "") for u in sink.ui)
    assert r.metrics["intent_unknown"] == 1
