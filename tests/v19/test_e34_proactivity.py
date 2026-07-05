"""E34 — Proactivity real & hot context device.

Real PC-side checks with a temp SQLite DB (no hardware, no cloud):

* **prediction → live** : an open ``predictions_v19`` row whose verification_spec
  matches the current scene → a proactive suggestion is really enqueued (evidence
  carried), using the outcome watcher's own ``_event_matches`` predicate;
* **clarification** : a queued ``v14_8`` question + a calm context → the question
  is delivered; an active conversation suppresses it;
* **dense retrieval** : Qdrant boundary mocked → a "similar_experiences" section
  appears in the HotSceneContext; boundary failure → clean empty degrade;
* **morning briefing** : first session of the day → one card; a second session the
  same day → deduplicated (no second card);
* **entity_hot_update** : identity_fusion naming a person → the prefetch message is
  emitted with a relation pack;
* **router NL-first** : a free natural sentence → LLM parse (frontier mocked) →
  correct intent; a high-confidence keyword command is always instant (no LLM);
  LLM off → the lenient grammar net still resolves.
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


worldbrain = _load("v19_worldbrain", "services/live-pc/worldbrain.py")
scene_adapter = _load("v19_scene_adapter", "services/live-pc/brainlive_scene_adapter.py")
proactive_context = _load("v19_proactive_context", "services/live-pc/proactive_context.py")
predictive_retrieval_live = _load("v19_predictive_retrieval_live", "services/live-pc/predictive_retrieval_live.py")
morning_briefing = _load("v19_morning_briefing", "services/live-pc/morning_briefing.py")
identity_fusion = _load("identity_fusion", "services/live-pc/identity_fusion.py")
intent_router = _load("v19_intent_router", "services/live-pc/intent_router.py")


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    return db_path


def _ent(track_id, label, bbox, conf=0.75, kind="object"):
    return {"track_id": track_id, "kind": kind, "label": label, "bbox": bbox,
            "confidence": conf, "visibility": 1.0, "age": 1}


def _delta(frame_id, entities, *, map_quality=0.0):
    return {"session_id": "s-e34", "source_frame_id": frame_id, "entities": entities,
            "relations": [], "changes": [], "map_quality": map_quality,
            "evidence_refs": [f"frame:{frame_id}"]}


def _wb(db_path, session_id="s-e34"):
    return worldbrain.WorldBrain(person_id="me", live_session_id=session_id, db_path=db_path,
                                 publish_world_state=False)


def _seed_open_prediction(db_path, *, person_id="me", entity_label="perceuse", package_date=None):
    """Insert a real open predictions_v19 row grounded in a verifiable spec."""
    from datetime import datetime, timezone

    from mlomega_audio_elite.v19_prediction_loop import ensure_prediction_schema
    from mlomega_audio_elite.db import connect, write_transaction, insert_only
    from mlomega_audio_elite.utils import json_dumps, now_iso, stable_id

    day = package_date or datetime.now(timezone.utc).date().isoformat()
    ensure_prediction_schema(db_path)
    spec = {
        "sources": ["visual_events_v19"],
        "entity_label": entity_label,
    }
    pid = stable_id("predv19", person_id, day, entity_label)
    now = now_iso()
    with connect(db_path) as con, write_transaction(con):
        insert_only(con, "predictions_v19", {
            "prediction_id": pid, "person_id": person_id, "emitted_at": now,
            "horizon_start": f"{day}T00:00:00+00:00", "horizon_end": f"{day}T23:59:59+00:00",
            "statement": f"Tu voulais racheter une {entity_label}.",
            "confidence": 0.7, "status": "open",
            "verification_spec_json": json_dumps(spec),
            "evidence_refs_json": json_dumps(["life_model:demo"]), "created_at": now,
        }, on_conflict="ignore")
    return pid


# --------------------------------------------------------------------------- §2a prediction↔scene
def test_open_prediction_matching_scene_enqueues_suggestion(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_open_prediction(db_path, entity_label="perceuse")

    wb = _wb(db_path)
    pc = proactive_context.ProactiveContext(person_id="me", db_path=db_path)
    pc.refresh()
    assert pc.metrics["predictions_open"] == 1, "the open prediction of the day should load"

    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-e34", worldbrain=wb, db_path=db_path, proactive=pc,
    )
    # A scene where the predicted entity ("perceuse") is present.
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "perceuse", [10, 10, 60, 80])]))

    results = adapter.evaluate_situations()
    queued = [r for r in results if r.get("status") == "queued"]
    assert adapter.metrics["proactive_predictions"] >= 1
    assert queued, "a proactive suggestion should be enqueued for the matching prediction"
    # The delivery really landed in the core queue with the statement + evidence.
    from mlomega_audio_elite.db import connect
    with connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM brainlive_intervention_delivery_queue WHERE live_session_id='s-e34'").fetchall()]
    assert any("perceuse" in (r.get("message") or "") for r in rows)


def test_prediction_no_match_no_suggestion(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_open_prediction(db_path, entity_label="perceuse")
    wb = _wb(db_path)
    pc = proactive_context.ProactiveContext(person_id="me", db_path=db_path)
    pc.refresh()
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-e34b", worldbrain=wb, db_path=db_path, proactive=pc,
    )
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "chaise", [10, 10, 60, 80])]))
    adapter.evaluate_situations()
    assert adapter.metrics["proactive_predictions"] == 0, "a non-matching scene must not fire the prediction"


# --------------------------------------------------------------------------- §2c clarification
class _FakeProactive:
    """A minimal ProactiveContext stand-in for the clarification timing test."""

    def __init__(self, question):
        self._clar = [{"item_id": "clar-1", "question_text": question}]
        self.delivered = []

    def snapshot(self):
        return {"predictions": [], "interventions": [],
                "clarifications": [{"id": "clar-1", "question": self._clar[0]["question_text"]}] if self._clar else []}

    def match_predictions(self, ctx):
        return []

    def relevant_interventions(self, ctx):
        return []

    def due_clarification(self, ctx, *, conversation_active):
        if conversation_active or not self._clar:
            return None
        return self._clar[0]

    def mark_clarification_delivered(self, item_id):
        self._clar = [c for c in self._clar if c.get("item_id") != item_id]
        self.delivered.append(item_id)


def test_clarification_delivered_only_when_calm(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    wb = _wb(db_path, session_id="s-clar")
    fake = _FakeProactive("Sarah, c'est bien ta sœur ?")
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-clar", worldbrain=wb, db_path=db_path, proactive=fake,
    )
    # Active conversation (transcript hint set) → the question is NOT asked.
    adapter.note_transcript("on parlait de tout autre chose là")
    adapter.evaluate_situations()
    assert adapter.metrics["clarifications_asked"] == 0, "never interrupt an active conversation"

    # Calm context (no transcript hint) → the question is delivered once.
    adapter.note_transcript("")
    adapter._transcript_hint = None
    results = adapter.evaluate_situations()
    assert adapter.metrics["clarifications_asked"] == 1
    assert "clar-1" in fake.delivered
    from mlomega_audio_elite.db import connect
    with connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM brainlive_intervention_delivery_queue WHERE live_session_id='s-clar'").fetchall()]
    assert any("sœur" in (r.get("message") or "") for r in rows)


# --------------------------------------------------------------------------- §3 dense retrieval
class _FakeBackend:
    """Mocks the Qdrant/reranker frontier: returns dict candidates."""

    def __init__(self, hits):
        self._hits = hits

    def retrieve(self, anchor, *, canonical_candidates, limit=None):
        assert anchor.get("embedding_text"), "anchor must carry the live subject text"
        return self._hits


def _seed_observed_case(db_path, person_id="me"):
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import ensure_longitudinal_case_schema
    from mlomega_audio_elite.db import connect, write_transaction, upsert
    from mlomega_audio_elite.utils import now_iso

    ensure_longitudinal_case_schema()
    now = now_iso()
    with connect(db_path) as con, write_transaction(con):
        upsert(con, "brain2_observed_cases_v17", {
            "observed_case_id": "case-1", "person_id": person_id, "case_type": "routine",
            "case_key": "bricolage", "title": "perceuse", "context_summary": "bricolage",
            "embedding_text": "j'ai racheté une perceuse au magasin",
            "observed_at": "2026-05-01T09:00:00+00:00", "created_at": now, "updated_at": now,
        }, "observed_case_id")


def test_similar_experiences_section_present_with_mocked_qdrant(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_observed_case(db_path)
    wb = _wb(db_path, session_id="s-sim")
    backend = _FakeBackend([{"text": "la dernière fois tu as racheté une perceuse", "score": 0.82}])
    retr = predictive_retrieval_live.PredictiveRetrievalLive(backend=backend, db_path=db_path)
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-sim", worldbrain=wb, db_path=db_path,
        predictive_retrieval=retr,
    )
    adapter.note_transcript("je cherche une perceuse")
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "perceuse", [0, 0, 40, 40])]))
    ctx = adapter.build_context()
    assert ctx.get("similar_experiences"), "the similar-experiences section should be folded into the hot context"
    assert "perceuse" in ctx["similar_experiences"][0]["text"]


def test_similar_experiences_degrades_when_qdrant_down(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_observed_case(db_path)
    wb = _wb(db_path, session_id="s-sim2")

    class _DownBackend:
        def retrieve(self, *a, **k):
            raise RuntimeError("Qdrant predictive search failed: connection refused")

    retr = predictive_retrieval_live.PredictiveRetrievalLive(backend=_DownBackend(), db_path=db_path)
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-sim2", worldbrain=wb, db_path=db_path,
        predictive_retrieval=retr,
    )
    adapter.note_transcript("je cherche une perceuse")
    ctx = adapter.build_context()  # must not raise
    assert not ctx.get("similar_experiences"), "a down Qdrant yields no section, no crash"
    assert retr.metrics["unavailable"] >= 1


# --------------------------------------------------------------------------- §6 morning briefing
def test_morning_briefing_first_session_then_deduped(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_open_prediction(db_path, entity_label="perceuse")

    # First session of the day.
    wb1 = _wb(db_path, session_id="s-morning-1")
    pc = proactive_context.ProactiveContext(person_id="me", db_path=db_path)
    mb1 = morning_briefing.MorningBriefing(
        person_id="me", live_session_id="s-morning-1", proactive=pc, worldbrain=wb1, db_path=db_path,
    )
    assert mb1.is_first_session_today() is True
    res1 = mb1.maybe_deliver()
    assert res1.get("status") == "queued", "first session of the day → briefing enqueued"

    from mlomega_audio_elite.db import connect
    with connect(db_path) as con:
        cards = [dict(r) for r in con.execute(
            "SELECT * FROM brainlive_intervention_delivery_queue WHERE message LIKE 'Bonjour%'").fetchall()]
    assert len(cards) == 1
    assert "perceuse" in cards[0]["message"]

    # A SECOND session the same day: an earlier session row now exists → skipped.
    mb2 = morning_briefing.MorningBriefing(
        person_id="me", live_session_id="s-morning-2", proactive=pc,
        worldbrain=_wb(db_path, session_id="s-morning-2"), db_path=db_path,
    )
    assert mb2.is_first_session_today() is False
    res2 = mb2.maybe_deliver()
    assert res2.get("status") == "skipped"
    with connect(db_path) as con:
        cards2 = [dict(r) for r in con.execute(
            "SELECT * FROM brainlive_intervention_delivery_queue WHERE message LIKE 'Bonjour%'").fetchall()]
    assert len(cards2) == 1, "no second briefing the same day"


# --------------------------------------------------------------------------- §5 entity_hot_update
def test_identity_naming_emits_entity_hot_update(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    wb = _wb(db_path, session_id="s-prefetch")
    emitted = []
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-prefetch", worldbrain=wb, db_path=db_path,
        on_entity_hot_update=emitted.append,
    )
    fusion = identity_fusion.IdentityFusion(worldbrain=wb, scene_adapter=adapter)
    # A confident face+voice agreement on the same person → named → prefetch fires.
    fusion.resolve(
        entity_id="e-sarah", track_id="t1",
        face={"matched": True, "person_id": "sarah", "name": "Sarah", "score": 0.8},
        voice={"matched": True, "person_id": "sarah", "name": "Sarah", "score": 0.8},
    )
    assert emitted, "naming a person should push an entity_hot_update to the device"
    msg = emitted[0]
    assert msg["type"] == "entity_hot_update"
    assert msg["entity_id"] == "e-sarah" and msg["person_id"] == "sarah" and msg["name"] == "Sarah"
    assert "relation_pack" in msg
    assert adapter.metrics["entity_hot_updates"] == 1
    # Idempotent within the session: a second resolve does not re-emit.
    fusion.resolve(entity_id="e-sarah", track_id="t1",
                   face={"matched": True, "person_id": "sarah", "name": "Sarah", "score": 0.8})
    assert adapter.metrics["entity_hot_updates"] == 1


# --------------------------------------------------------------------------- §1 router NL-first
class _StubLLM:
    """A live LLM stub: records calls and returns a scripted intent JSON."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user, *, schema_hint=None, timeout=None):
        self.calls.append(user)
        return dict(self.reply)

    # paid/local switch handlers (not the NL parse path — must not touch self.calls)
    def switch_to_cloud(self, provider="openai", **kw):
        return {"ok": True, "provider": provider, "cloud_active": True, "text": "mode payant"}

    def switch_to_local(self):
        return {"ok": True, "cloud_active": False, "text": "mode local"}


class _Sink:
    def __init__(self):
        self.ui = []
        self.device = []
        self.vision = []

    def emit_ui(self, i):
        self.ui.append(i)

    def emit_device(self, c):
        self.device.append(c)

    def vision_focus(self, r):
        self.vision.append(r)
        return {"kind": r.get("kind")}


def _router(sink, *, llm=None):
    return intent_router.IntentRouter(
        vision_focus=sink.vision_focus, on_device_command=sink.emit_device,
        ask_memory=lambda q: {"content": {"text": "x"}}, llm_router=llm,
        emit_ui_intent=sink.emit_ui,
    )


def test_natural_sentence_goes_to_llm_first():
    # A free natural sentence (no leading keyword) → the LLM parses it, not grammar.
    llm = _StubLLM({"intent": "replay", "time": "14h"})
    sink = _Sink()
    r = _router(sink, llm=llm)
    out = r.on_transcript("tu peux me montrer ce que j'ai fait vers 14h ?")
    assert out["intent"] == "replay"
    assert out["device_command"]["time"] == "14h"
    assert llm.calls, "the natural sentence must reach the LLM parse (NL-first)"
    assert r.metrics["llm_fallbacks"] == 1


def test_high_confidence_keyword_is_instant_no_llm():
    # A command that BEGINS with an exact keyword is instant — the LLM is untouched.
    llm = _StubLLM({"intent": "unknown"})
    sink = _Sink()
    r = _router(sink, llm=llm)
    for text, expect in [("cache tout", "set_ui_mode"), ("menu", "menu"), ("zoom", "zoom"),
                         ("mode payant openai", "paid_mode"), ("mode local", "local_mode")]:
        out = r.on_transcript(text)
        assert out["intent"] == expect, f"{text!r} should be instant → {expect}, got {out['intent']}"
    assert not llm.calls, "high-confidence keyword commands must never call the LLM"
    assert r.metrics["grammar_hits"] >= 5


def test_lenient_grammar_net_when_llm_off():
    # No LLM available → the full lenient grammar still resolves free-ish orders.
    sink = _Sink()
    r = _router(sink, llm=None)
    out = r.on_transcript("trouve mes clés")
    assert out["intent"] == "find"
    assert out["request"]["query"] == "mes clés"
    assert r.metrics["grammar_hits"] >= 1
