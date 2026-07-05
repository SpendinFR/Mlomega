"""E38 — Intelligence fine: identity hypotheses + bi-modal attribute changes +
learned routine→object associations.

Real PC-side checks with in-memory / temp SQLite stores (no hardware, no cloud).
The LLM frontiers (addressed-name extraction, heard-fact extraction) are mocked at
their REAL strict-JSON shape. RÈGLE D'OR: nothing is hardcoded to a name/role/
attribute — every test uses arbitrary, varied values to prove the mechanisms are
generic; the examples live ONLY here.

* hypothesis: 3 concordant "addressing" turns across 2 sessions → auto-promotion +
  a discreet announcement UIIntent; a contradiction → no promotion; a correction →
  the hypothesis is broken;
* attribute: an OCR value in session 1, a DIFFERENT heard value in session 2 (same
  place) → an ``attribute_changed`` ChangeEvent carrying BOTH sources (bi-modal);
* person appearance: differing descriptors across sessions → ``attribute_changed``
  via the same mechanism;
* routine→object: seeded co-occurrences → approaching the zone pushes the last-seen
  of the associated object (and not an unrelated one);
* genericity: arbitrary keys/values throughout.
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


hypothesis_engine = _load("v19_hypothesis_engine", "services/live-pc/hypothesis_engine.py")
attribute_memory = _load("v19_attribute_memory", "services/live-pc/attribute_memory.py")
routine_associations = _load("v19_routine_associations", "services/live-pc/routine_associations.py")
worldbrain = _load("v19_worldbrain", "services/live-pc/worldbrain.py")


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    return db_path


class FakeLLM:
    """Mock LLM frontier: returns queued JSON dicts in order, at the real shape."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def complete_json(self, system, user, *, schema_hint=None, timeout=None):
        self.calls.append(user)
        if not self._replies:
            return {}
        return self._replies.pop(0)


class FakeEntity:
    def __init__(self, entity_id, label="person", lifecycle="confirmed", person_id=None):
        self.entity_id = entity_id
        self.label = label
        self.lifecycle = lifecycle
        self.person_id = person_id
        self.person_name = None


class FakeWorld:
    def __init__(self, entities=None):
        self.entities = entities or {}
        self.attribute_changes = []

    def record_attribute_change(self, *, subject, attribute, before, after, evidence_refs=None):
        change = {"type": "attribute_changed", "subject": subject, "attribute": attribute,
                  "before": before, "after": after, "evidence": list(evidence_refs or [])}
        self.attribute_changes.append(change)
        return change


# ============================================================ §1 hypotheses
def test_addressed_name_promotes_across_sessions_with_announcement():
    """3 concordant addressing turns over 2 sessions → auto-promotion + announce."""
    # Arbitrary invented name — proves genericity (no lexicon).
    NAME = "Zoltar"
    ENT = "ent-person-42"
    world = FakeWorld({ENT: FakeEntity(ENT)})
    # Only the addressing turns return addressed=True; the present person's own
    # turns return addressed=False. The addressee heuristic binds the name to the
    # previous speaker (the present person who just spoke).
    NO = {"addressed": False, "name": None, "addressee": "unknown", "confidence": 0.0}
    YES = lambda c: {"addressed": True, "name": NAME, "addressee": "previous_speaker", "confidence": c}
    llm = FakeLLM([NO, YES(0.7), NO, YES(0.6), NO, YES(0.65)])
    announced = []
    eng = hypothesis_engine.HypothesisEngine(
        llm=llm, worldbrain=world, on_ui_intent=announced.append,
        config=hypothesis_engine.HypothesisConfig(min_occurrences=3, min_sessions=2, min_cumulative_confidence=1.2),
    )
    # Session A: two addressing turns; session B: one. 3 concordant obs / 2 sessions.
    eng.note_turn("...", session="sessA", speaker_entity=ENT, present_person_entities=[ENT])
    eng.note_turn("merci beaucoup", session="sessA", speaker_entity="me", present_person_entities=[ENT])
    eng.note_turn("...", session="sessA", speaker_entity=ENT, present_person_entities=[ENT])
    eng.note_turn("exactement", session="sessA", speaker_entity="me", present_person_entities=[ENT])
    eng.note_turn("...", session="sessB", speaker_entity=ENT, present_person_entities=[ENT])
    eng.note_turn("à bientôt alors", session="sessB", speaker_entity="me", present_person_entities=[ENT])

    hyps = eng.active_hypotheses(entity_id=ENT)
    assert any(h["value"] == NAME and h["status"] == "promoted" for h in hyps), hyps
    # NEVER silent: an announcement fired, and it is correctable.
    assert announced, "promotion must announce (never silent)"
    ann = announced[-1]
    assert ann["kind"] == "hypothesis_promoted" and ann["value"] == NAME
    assert ann["correctable"] is True and NAME in ann["text"]
    # The entity carries the promoted name at observed truth.
    assert world.entities[ENT].person_name == NAME
    assert eng.metrics["auto_promotions"] == 1


def test_contradiction_prevents_promotion():
    """Concordant signals for two competing values do not promote either."""
    ENT = "ent-p-7"
    world = FakeWorld({ENT: FakeEntity(ENT)})
    # Alternating competing names for the same person → each weakens the other.
    NO = {"addressed": False, "name": None, "addressee": "unknown", "confidence": 0.0}
    def YES(n):
        return {"addressed": True, "name": n, "addressee": "previous_speaker", "confidence": 0.6}
    names = ["Qorin", "Vashti", "Qorin", "Vashti"]
    llm = FakeLLM([x for n in names for x in (NO, YES(n))])
    eng = hypothesis_engine.HypothesisEngine(
        llm=llm, worldbrain=world,
        config=hypothesis_engine.HypothesisConfig(min_occurrences=2, min_sessions=2, min_cumulative_confidence=1.0),
    )
    for i in range(4):
        eng.note_turn("...", session=f"s{i}", speaker_entity=ENT, present_person_entities=[ENT])
        eng.note_turn("x", session=f"s{i}", speaker_entity="me", present_person_entities=[ENT])
    assert eng.metrics["auto_promotions"] == 0
    assert world.entities[ENT].person_name is None


def test_correction_breaks_promoted_hypothesis():
    ENT = "ent-p-9"
    world = FakeWorld({ENT: FakeEntity(ENT)})
    NAME = "Ткач"  # non-latin, arbitrary — genericity
    NO = {"addressed": False, "name": None, "addressee": "unknown", "confidence": 0.0}
    YES = {"addressed": True, "name": NAME, "addressee": "previous_speaker", "confidence": 0.9}
    llm = FakeLLM([NO, YES, NO, YES])
    eng = hypothesis_engine.HypothesisEngine(
        llm=llm, worldbrain=world,
        config=hypothesis_engine.HypothesisConfig(min_occurrences=2, min_sessions=2, min_cumulative_confidence=0.8),
    )
    for i in range(2):
        eng.note_turn("...", session=f"c{i}", speaker_entity=ENT, present_person_entities=[ENT])
        eng.note_turn("y", session=f"c{i}", speaker_entity="me", present_person_entities=[ENT])
    assert world.entities[ENT].person_name == NAME
    # E32 voice correction breaks the entity's hypotheses.
    eng.break_hypotheses_for_entity(ENT)
    assert world.entities[ENT].person_name is None
    assert not any(h["status"] == "promoted" for h in eng.active_hypotheses(entity_id=ENT))


def test_no_signal_when_no_present_person():
    """LLM extracts a name but no present person → observation dropped."""
    world = FakeWorld({})
    llm = FakeLLM([{"addressed": True, "name": "Nemo", "addressee": "unknown", "confidence": 0.9}])
    eng = hypothesis_engine.HypothesisEngine(llm=llm, worldbrain=world)
    res = eng.note_turn("bonjour Nemo", session="s", speaker_entity=None, present_person_entities=[])
    assert res is None
    assert eng.metrics["observations_added"] == 0


def test_clarification_bridge_records_resolution_without_touching_core(tmp_path):
    """A promoted name matching a core pending hypothesis → service-side resolution."""
    ENT = "ent-p-11"
    world = FakeWorld({ENT: FakeEntity(ENT)})
    NAME = "Brixby"
    NO = {"addressed": False, "name": None, "addressee": "unknown", "confidence": 0.0}
    YES = {"addressed": True, "name": NAME, "addressee": "previous_speaker", "confidence": 0.9}
    llm = FakeLLM([NO, YES, NO, YES])
    pending = [{"hypothesis_id": "core-hyp-1", "candidate_name": NAME}]
    eng = hypothesis_engine.HypothesisEngine(
        llm=llm, worldbrain=world,
        service_db_path=str(tmp_path / "hyp.db"),
        clarification_reader=lambda *, person_id: pending,
        config=hypothesis_engine.HypothesisConfig(min_occurrences=2, min_sessions=2, min_cumulative_confidence=0.8),
    )
    for i in range(2):
        eng.note_turn("...", session=f"r{i}", speaker_entity=ENT, present_person_entities=[ENT])
        eng.note_turn("z", session=f"r{i}", speaker_entity="me", present_person_entities=[ENT])
    resolutions = eng.resolve_clarifications()
    assert resolutions and resolutions[0]["core_hypothesis_id"] == "core-hyp-1"
    assert resolutions[0]["value"] == NAME
    assert eng.metrics["clarifications_resolved"] == 1


# ============================================================ §2 attribute changes
def test_bimodal_ocr_then_heard_attribute_change(tmp_path, monkeypatch):
    """Session 1: OCR value. Session 2: DIFFERENT heard value (same place) →
    attribute_changed carrying both sources (a SEEN value contradicted by a HEARD)."""
    _env(tmp_path, monkeypatch)
    world = worldbrain.WorldBrain(person_id="me", live_session_id="ls", db_path=tmp_path / "memory.db")
    # Heard-fact LLM: session 2 states a differing value for the same attribute.
    llm = FakeLLM([
        {"states_fact": True, "subject_hint": None, "attribute": "affiche", "value": "42", "confidence": 0.8},
    ])
    mem = attribute_memory.AttributeMemory(worldbrain=world, llm=llm, service_db_path=str(tmp_path / "attr.db"))
    PLACE = "zone::comptoir_nord"
    # Session 1 — an OCR reading "affiche: 17" at the place (generic key:value split).
    ch1 = mem.observe_ocr(subject=PLACE, readings=[{"text": "affiche: 17"}], session="s1")
    assert ch1 == []  # first sighting, no change
    # Session 2 — a heard fact "l'affiche indique 42" resolved to the same place.
    ch2 = mem.note_turn("...", session="s2", default_subject=PLACE)
    assert ch2 is not None, "differing value across sessions must yield a change"
    assert ch2["type"] == "attribute_changed"
    assert ch2["before"]["value"] == "17" and ch2["before"]["source"] == "ocr"
    assert ch2["after"]["value"] == "42" and ch2["after"]["source"] == "heard"
    assert mem.metrics["attribute_changes"] == 1
    # WorldBrain recorded the ChangeEvent (two sources → observed truth).
    assert any(c.change_type == "attribute_changed" for c in world.change_events)


def test_same_value_no_change(tmp_path):
    world = FakeWorld({})
    mem = attribute_memory.AttributeMemory(worldbrain=world, service_db_path=str(tmp_path / "a.db"))
    SUBJ = "thing::x99"
    mem.observe(subject=SUBJ, attribute="etat", value="ouvert", source="vlm", session="a")
    ch = mem.observe(subject=SUBJ, attribute="etat", value="ouvert", source="ocr", session="b")
    assert ch is None
    assert mem.metrics["attribute_changes"] == 0


def test_person_appearance_change_same_mechanism(tmp_path):
    """Different appearance descriptors across sessions → attribute_changed."""
    world = FakeWorld({})
    mem = attribute_memory.AttributeMemory(worldbrain=world, service_db_path=str(tmp_path / "ap.db"))
    ENT = "ent-person-known-3"
    mem.observe_person_appearance(
        entity_id=ENT, descriptor={"coiffure": "attachée", "haut": "rayé bleu"}, session="d1")
    changes = mem.observe_person_appearance(
        entity_id=ENT, descriptor={"coiffure": "courte", "haut": "rayé bleu"}, session="d2")
    # coiffure changed → one attribute_changed; haut unchanged → none.
    assert len(changes) == 1
    assert changes[0]["attribute"] == "coiffure"
    assert changes[0]["before"]["value"] == "attachée"
    assert changes[0]["after"]["value"] == "courte"


# ============================================================ §3 routine→object
def _seed_visual_and_routines(db_path, person_id="me"):
    from mlomega_audio_elite import v19_visual_store as store  # type: ignore
    from mlomega_audio_elite.db import connect  # type: ignore

    store.ensure_v19_visual_schema(db_path)
    # Two zones, two distinct objects each seen repeatedly in their own zone.
    seeds = [
        ("zoneA", "ent-objA", "widget", 3),
        ("zoneB", "ent-objB", "gadget", 3),
    ]
    for zone, eid, label, n in seeds:
        for i in range(n):
            store.store_visual_event({
                "memory_owner_id": person_id, "live_session_id": f"seed-{zone}-{i}",
                "event_type": "entity_last_seen", "occurred_at": f"2026-06-0{i+1}T10:00:00+00:00",
                "entity": {"entity_id": eid, "kind": "object", "label": label},
                "place": {"place_key": zone}, "truth_level": "observed", "confidence": 0.9,
            }, db_path=db_path)
    # A routine model per zone (real v19 table columns — created by the schema call).
    now = "2026-06-10T00:00:00+00:00"
    with connect(db_path) as con:
        for rid, ekey, zone, slot in (("m1", "routineA", "zoneA", "morning"),
                                      ("m2", "routineB", "zoneB", "evening")):
            con.execute(
                """INSERT INTO brain2_spatial_routine_models(
                     routine_id, person_id, entity_key, place_key, time_slot,
                     occurrence_count, confidence, updated_at, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (rid, person_id, ekey, zone, slot, 5, 0.8, now, now))
        con.commit()


class FakeSceneAdapter:
    def __init__(self):
        self.pushed = []
        self.suggestions = []
        self._on_entity_hot_update = self.suggestions.append

    def push_object_hot(self, entity):
        self.pushed.append(entity)
        return entity


def test_routine_approach_pushes_associated_object_only(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    db_path = tmp_path / "memory.db"
    _seed_visual_and_routines(db_path)
    adapter = FakeSceneAdapter()
    assoc = routine_associations.RoutineAssociations(
        db_path=db_path, scene_adapter=adapter,
        config=routine_associations.AssociationConfig(min_score=0.3, min_cooccurrence=2),
    )
    learned = assoc.learn()
    assert "zonea" in {k.lower() for k in learned}
    # Approaching zoneA pushes widget (its associated object), not gadget.
    pushes = assoc.on_approach(place_key="zoneA", visible_labels=[])
    labels = {p["object_label"] for p in pushes}
    assert "widget" in labels and "gadget" not in labels
    assert adapter.pushed and adapter.pushed[0]["label"] == "widget"
    assert assoc.metrics["routine_pushes"] == 1
    # Object not visible → a discreet suggestion was emitted.
    assert any(s.get("kind") == "routine_object_suggestion" for s in adapter.suggestions)


def test_routine_approach_deduped_per_session(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    db_path = tmp_path / "memory.db"
    _seed_visual_and_routines(db_path)
    adapter = FakeSceneAdapter()
    assoc = routine_associations.RoutineAssociations(db_path=db_path, scene_adapter=adapter)
    assoc.learn()
    assoc.on_approach(place_key="zoneB", visible_labels=[])
    n = assoc.metrics["routine_pushes"]
    assoc.on_approach(place_key="zoneB", visible_labels=[])  # second approach, same session
    assert assoc.metrics["routine_pushes"] == n  # no re-push


def test_visible_object_no_suggestion(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    db_path = tmp_path / "memory.db"
    _seed_visual_and_routines(db_path)
    adapter = FakeSceneAdapter()
    assoc = routine_associations.RoutineAssociations(db_path=db_path, scene_adapter=adapter)
    assoc.learn()
    # widget already visible → pushed (cache) but NO suggestion.
    assoc.on_approach(place_key="zoneA", visible_labels=["widget"])
    assert not any(s.get("kind") == "routine_object_suggestion" for s in adapter.suggestions)
    assert assoc.metrics["suggestions"] == 0
