from __future__ import annotations

"""HypothesisEngine — auto-confirmation of identity hypotheses (E38 §1).

The live pipeline sees a *person* entity long before it can name it. Face/voice
identity (E32) names it when a cue fires; the StrangerProfiler (E36) attaches a
name-less "? role" hypothesis when it does not. What was missing is the *third*
path to a name: **the conversation itself**. When a name is spoken and addressed
to a present person ("tu as raison, Karim"), that is evidence — weak on its own,
but **accumulated across sessions** it converges. This engine turns that stream
of weak signals into a promotable identity hypothesis.

Nothing here is hardcoded to any particular name, role, or attribute:

* The **addressed-name signal** comes from a *generic LLM extraction* on final
  turns — "is a present person being addressed/named in this turn?" — returning
  strict JSON. There is NO name lexicon and NO name regex; the LLM frontier is a
  single injectable callable (mocked in tests at the real JSON shape).
* The **association** name→person is a documented heuristic: the candidate name
  is bound to the most plausible present person entity — the *previous speaker*
  (the addressee of "tu … , <name>" is usually whoever just spoke) falling back
  to the single active person track. The heuristic never invents a binding when
  the scene is ambiguous (no present person → the observation is dropped).
* The **store** (service-local SQLite, never a core table) accumulates
  observations per hypothesis: each concordant observation *reinforces*
  (occurrence + cumulative confidence), each contradiction *weakens*.
* **Promotion** is threshold-driven (``min_occurrences`` independent observations
  × ``min_cumulative_confidence``). On promotion the hypothesis becomes an
  attribute of the WorldBrain entity (``truth_level`` probable→observed) and —
  NEVER silently — a discreet UIIntent announces the deduction ("J'ai déduit :
  c'est probablement <valeur> — corrige-moi si faux"). Below threshold it stays a
  displayed hypothesis (§17.2). A voice correction (E32) breaks/weakens it.

The engine also *reads* the core's ``clarification_inbox`` identity hypotheses
(``v14_5_people_identity_hypotheses`` / UNKNOWN_VOICE). When its own signals
converge on one of those, it records a machine-sourced resolution service-side
with evidence — it does NOT mutate the core (ADR §E38: the core
``answer_clarification`` path is an LLM-interpreted *spoken* answer, not a clean
programmatic resolution hook; writing a machine resolution through it would
fabricate a user utterance). The manual enrollment (E32 ``fuse``) stays the
shortcut and always wins.
"""

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The LLM is asked a GENERIC question: is a present person addressed/named in this
# turn? No name lexicon — the model reads the natural language. Strict JSON out.
_ADDRESS_PROMPT_SYSTEM = (
    "You extract, from ONE conversation turn, whether a person who is physically "
    "present is being addressed or named. Do not guess names that are not stated. "
    "Answer strict JSON only with keys: addressed (bool), name (string or null), "
    "addressee (one of 'previous_speaker'|'current_speaker'|'unknown'), "
    "confidence (0..1). If no present person is named/addressed, addressed=false."
)
_ADDRESS_SCHEMA = {
    "addressed": "bool",
    "name": "string|null",
    "addressee": "previous_speaker|current_speaker|unknown",
    "confidence": "0..1",
}


@dataclass
class HypothesisConfig:
    """Promotion thresholds + signal floors — all config, never hardcoded."""

    min_occurrences: int = 3            # concordant observations needed
    min_sessions: int = 2               # spread over at least this many sessions
    min_cumulative_confidence: float = 1.2  # summed confidence to promote
    min_signal_confidence: float = 0.35     # a single observation must clear this
    contradiction_penalty: float = 0.5      # confidence removed per contradiction
    max_sessions_window: int = 0            # 0 = unbounded multi-session accumulation


@dataclass
class Observation:
    """One dated piece of evidence for/against a hypothesis."""

    session: str
    source: str            # heard | vlm | context
    confidence: float
    evidence_ref: str | None = None
    concordant: bool = True
    observed_at: str = field(default_factory=_iso_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "source": self.source,
            "confidence": round(float(self.confidence), 4),
            "evidence_ref": self.evidence_ref,
            "concordant": self.concordant,
            "observed_at": self.observed_at,
        }


@dataclass
class Hypothesis:
    """A provisional attribute (name|role|other) about a person/entity."""

    hypothesis_id: str
    entity_id: str | None
    attr_type: str         # name | role | <free attribute key>
    value: str
    occurrences: list[Observation] = field(default_factory=list)
    status: str = "hypothesis"   # hypothesis | promoted | broken
    promoted_at: str | None = None

    def sessions(self) -> set[str]:
        return {o.session for o in self.occurrences if o.concordant}

    def cumulative_confidence(self) -> float:
        total = 0.0
        for o in self.occurrences:
            total += float(o.confidence) if o.concordant else -abs(float(o.confidence))
        return total

    def concordant_count(self) -> int:
        return sum(1 for o in self.occurrences if o.concordant)

    def independent_count(self) -> int:
        """Independent = distinct sessions with a concordant observation."""
        return len(self.sessions())

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "entity_id": self.entity_id,
            "attr_type": self.attr_type,
            "value": self.value,
            "status": self.status,
            "occurrences": [o.to_dict() for o in self.occurrences],
            "independent_count": self.independent_count(),
            "cumulative_confidence": round(self.cumulative_confidence(), 4),
            "promoted_at": self.promoted_at,
        }


def _norm_value(value: Any) -> str:
    return str(value or "").strip()


def _hyp_key(entity_id: str | None, attr_type: str, value: str) -> str:
    return f"{entity_id or '?'}|{attr_type}|{_norm_value(value).lower()}"


class HypothesisEngine:
    """Accumulates identity/attribute hypotheses and auto-promotes at threshold."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        llm: Any = None,                       # any object with complete_json(...)
        worldbrain: Any = None,
        config: HypothesisConfig | None = None,
        service_db_path: str | Path | None = None,
        on_ui_intent: Callable[[dict[str, Any]], Any] | None = None,
        clarification_reader: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
        db_path: Any = None,
    ) -> None:
        self.person_id = person_id
        self.llm = llm
        self.worldbrain = worldbrain
        self.config = config or HypothesisConfig()
        self._on_ui = on_ui_intent
        self._clar_reader = clarification_reader
        self.db_path = db_path
        # hyp_key -> Hypothesis (in-memory mirror of the store)
        self.hypotheses: dict[str, Hypothesis] = {}
        # rolling short conversation memory for the addressee heuristic
        self._last_speaker_entity: str | None = None
        self._prev_speaker_entity: str | None = None
        self._hyp_seq = 0
        self._svc_db = self._init_service_db(service_db_path)
        self._load_from_store()
        self.metrics = {
            "turns_seen": 0,
            "signals_extracted": 0,
            "observations_added": 0,
            "contradictions": 0,
            "auto_promotions": 0,
            "clarifications_resolved": 0,
            "llm_unavailable": 0,
        }

    # -------------------------------------------------------- service-local store
    def _init_service_db(self, path: str | Path | None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path) if path else ":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS hypothesis_engine_hypotheses(
                 hypothesis_id TEXT PRIMARY KEY, person_id TEXT, entity_id TEXT,
                 attr_type TEXT, value TEXT, status TEXT, promoted_at TEXT,
                 occurrences_json TEXT, updated_at TEXT)"""
        )
        # Machine-sourced clarification resolutions (NOT written into the core).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS hypothesis_engine_resolutions(
                 resolution_id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT,
                 core_hypothesis_id TEXT, entity_id TEXT, attr_type TEXT, value TEXT,
                 source TEXT, evidence_json TEXT, resolved_at TEXT)"""
        )
        conn.commit()
        return conn

    def _load_from_store(self) -> None:
        try:
            rows = self._svc_db.execute(
                "SELECT * FROM hypothesis_engine_hypotheses WHERE person_id=?",
                (self.person_id,),
            ).fetchall()
        except Exception:
            return
        for r in rows:
            occ = []
            for o in json.loads(r["occurrences_json"] or "[]"):
                occ.append(Observation(
                    session=o.get("session", ""), source=o.get("source", "heard"),
                    confidence=float(o.get("confidence") or 0.0),
                    evidence_ref=o.get("evidence_ref"), concordant=bool(o.get("concordant", True)),
                    observed_at=o.get("observed_at") or _iso_now(),
                ))
            h = Hypothesis(
                hypothesis_id=r["hypothesis_id"], entity_id=r["entity_id"],
                attr_type=r["attr_type"], value=r["value"], occurrences=occ,
                status=r["status"] or "hypothesis", promoted_at=r["promoted_at"],
            )
            self.hypotheses[_hyp_key(h.entity_id, h.attr_type, h.value)] = h

    def _persist(self, h: Hypothesis) -> None:
        self._svc_db.execute(
            """INSERT INTO hypothesis_engine_hypotheses(
                 hypothesis_id, person_id, entity_id, attr_type, value, status,
                 promoted_at, occurrences_json, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(hypothesis_id) DO UPDATE SET
                 entity_id=excluded.entity_id, status=excluded.status,
                 promoted_at=excluded.promoted_at,
                 occurrences_json=excluded.occurrences_json, updated_at=excluded.updated_at""",
            (
                h.hypothesis_id, self.person_id, h.entity_id, h.attr_type, h.value,
                h.status, h.promoted_at,
                json.dumps([o.to_dict() for o in h.occurrences]), _iso_now(),
            ),
        )
        self._svc_db.commit()

    # -------------------------------------------------------- conversation intake
    def note_turn(
        self,
        text: str,
        *,
        session: str,
        speaker_entity: str | None = None,
        present_person_entities: Sequence[str] | None = None,
    ) -> Hypothesis | None:
        """Ingest one FINAL conversation turn.

        Runs the generic LLM addressed-name extraction, binds the candidate name
        to the most plausible present person entity (addressee heuristic), and
        records a ``heard`` observation. Returns the (possibly promoted) hypothesis
        or None when no present person is named. Updates the rolling speaker memory
        used by the heuristic.
        """
        self.metrics["turns_seen"] += 1
        text = (text or "").strip()
        # Update rolling speaker memory BEFORE resolving the addressee: the current
        # turn's speaker becomes "previous" for the NEXT turn.
        prev_speaker = self._last_speaker_entity
        signal = self._extract_addressed_name(text) if text else None
        result: Hypothesis | None = None
        if signal and signal.get("addressed") and _norm_value(signal.get("name")):
            self.metrics["signals_extracted"] += 1
            entity = self._resolve_addressee(
                addressee=str(signal.get("addressee") or "unknown"),
                prev_speaker=prev_speaker,
                current_speaker=speaker_entity,
                present=list(present_person_entities or []),
            )
            if entity is not None:
                conf = max(float(signal.get("confidence") or 0.0), 0.0)
                result = self.observe(
                    entity_id=entity, attr_type="name",
                    value=_norm_value(signal.get("name")), source="heard",
                    session=session, confidence=conf,
                    evidence_ref=f"turn:{session}",
                )
        # advance speaker memory
        if speaker_entity is not None:
            self._prev_speaker_entity = self._last_speaker_entity
            self._last_speaker_entity = speaker_entity
        return result

    def _extract_addressed_name(self, text: str) -> dict[str, Any] | None:
        """Generic LLM extraction (mockable). Never a name lexicon/regex."""
        if self.llm is None:
            return None
        try:
            data = self.llm.complete_json(
                _ADDRESS_PROMPT_SYSTEM, text, schema_hint=_ADDRESS_SCHEMA,
            )
        except Exception:
            self.metrics["llm_unavailable"] += 1
            return None
        if not isinstance(data, Mapping):
            return None
        return dict(data)

    def _resolve_addressee(
        self, *, addressee: str, prev_speaker: str | None,
        current_speaker: str | None, present: list[str],
    ) -> str | None:
        """Heuristic name→person binding (documented, never invented).

        "tu … , <name>" addresses whoever the speaker is talking TO — usually the
        person who just spoke (previous speaker). We therefore prefer:
        1. the explicit addressee hint from the LLM (previous/current speaker);
        2. else the previous speaker;
        3. else, if exactly one present person, that person;
        Ambiguous scene (multiple present, no speaker signal) → None (dropped).
        """
        if addressee == "current_speaker" and current_speaker:
            return current_speaker
        if addressee == "previous_speaker" and prev_speaker:
            return prev_speaker
        if prev_speaker and prev_speaker in present:
            return prev_speaker
        if prev_speaker:
            return prev_speaker
        distinct = [e for e in dict.fromkeys(present)]
        if len(distinct) == 1:
            return distinct[0]
        return None

    # -------------------------------------------------------- generic observe
    def observe(
        self,
        *,
        entity_id: str | None,
        attr_type: str,
        value: str,
        source: str,
        session: str,
        confidence: float,
        evidence_ref: str | None = None,
    ) -> Hypothesis | None:
        """Record ONE concordant observation for an attribute hypothesis.

        Reinforces an existing hypothesis (or creates one). A promoted or broken
        hypothesis is not re-created. Auto-promotes when thresholds are met.
        Returns the hypothesis, or None if the signal is below the floor.
        """
        value = _norm_value(value)
        if not value or float(confidence) < self.config.min_signal_confidence:
            return None
        key = _hyp_key(entity_id, attr_type, value)
        h = self.hypotheses.get(key)
        if h is None:
            self._hyp_seq += 1
            h = Hypothesis(
                hypothesis_id=f"hyp-{self.person_id}-{self._hyp_seq}-{attr_type}",
                entity_id=entity_id, attr_type=attr_type, value=value,
            )
            self.hypotheses[key] = h
        if h.status == "broken":
            return h
        h.occurrences.append(Observation(
            session=session, source=source, confidence=float(confidence),
            evidence_ref=evidence_ref, concordant=True,
        ))
        self.metrics["observations_added"] += 1
        # A concordant observation for one value is a CONTRADICTION of any other
        # value of the same (entity, attr_type) — weaken the competitors.
        self._weaken_competitors(entity_id, attr_type, value, session)
        self._persist(h)
        self._maybe_promote(h)
        return h

    def _weaken_competitors(
        self, entity_id: str | None, attr_type: str, value: str, session: str
    ) -> None:
        for k, other in self.hypotheses.items():
            if other.entity_id != entity_id or other.attr_type != attr_type:
                continue
            if _norm_value(other.value).lower() == _norm_value(value).lower():
                continue
            if other.status == "promoted":
                continue
            other.occurrences.append(Observation(
                session=session, source="context",
                confidence=self.config.contradiction_penalty,
                evidence_ref="competing_value", concordant=False,
            ))
            self.metrics["contradictions"] += 1
            self._persist(other)

    def contradict(
        self, *, entity_id: str | None, attr_type: str, value: str,
        session: str, confidence: float | None = None, evidence_ref: str | None = None,
    ) -> Hypothesis | None:
        """Record a contradicting observation (weakens the hypothesis).

        Used by the E32 voice correction path ("non, ce n'est pas X") and by any
        cue that disputes an attribute. A promoted hypothesis that is contradicted
        is BROKEN (the deduction was wrong)."""
        key = _hyp_key(entity_id, attr_type, value)
        h = self.hypotheses.get(key)
        if h is None:
            return None
        pen = self.config.contradiction_penalty if confidence is None else abs(float(confidence))
        h.occurrences.append(Observation(
            session=session, source="context", confidence=pen,
            evidence_ref=evidence_ref or "correction", concordant=False,
        ))
        self.metrics["contradictions"] += 1
        if h.status == "promoted":
            h.status = "broken"
            self._demote_entity(h)
        elif h.cumulative_confidence() <= 0:
            h.status = "broken"
        self._persist(h)
        return h

    def break_hypotheses_for_entity(self, entity_id: str, *, session: str = "correction") -> int:
        """Break every hypothesis on an entity (hard voice correction)."""
        n = 0
        for h in self.hypotheses.values():
            if h.entity_id == entity_id and h.status != "broken":
                h.occurrences.append(Observation(
                    session=session, source="context",
                    confidence=self.config.contradiction_penalty,
                    evidence_ref="entity_correction", concordant=False,
                ))
                was_promoted = h.status == "promoted"
                h.status = "broken"
                if was_promoted:
                    self._demote_entity(h)
                self._persist(h)
                n += 1
        self.metrics["contradictions"] += n
        return n

    # -------------------------------------------------------- promotion
    def _maybe_promote(self, h: Hypothesis) -> None:
        if h.status != "hypothesis":
            return
        cfg = self.config
        if (h.concordant_count() >= cfg.min_occurrences
                and h.independent_count() >= cfg.min_sessions
                and h.cumulative_confidence() >= cfg.min_cumulative_confidence):
            self._promote(h)

    def _promote(self, h: Hypothesis) -> None:
        h.status = "promoted"
        h.promoted_at = _iso_now()
        self.metrics["auto_promotions"] += 1
        self._apply_to_entity(h)
        self._announce(h)
        self._persist(h)

    def _apply_to_entity(self, h: Hypothesis) -> None:
        """Write the promoted attribute onto the WorldBrain entity (observed)."""
        if not h.entity_id or self.worldbrain is None:
            return
        ent = getattr(self.worldbrain, "entities", {}).get(h.entity_id)
        if ent is None:
            return
        try:
            attrs = dict(getattr(ent, "hypothesis_attributes", {}) or {})
            attrs[h.attr_type] = {"value": h.value, "truth_level": "observed",
                                  "source": "hypothesis_engine"}
            ent.hypothesis_attributes = attrs  # type: ignore[attr-defined]
            # A promoted NAME becomes the entity's name (still correctable).
            if h.attr_type == "name":
                ent.person_name = h.value       # type: ignore[attr-defined]
                ent.name_truth_level = "observed"  # type: ignore[attr-defined]
        except Exception:
            pass

    def _demote_entity(self, h: Hypothesis) -> None:
        if not h.entity_id or self.worldbrain is None:
            return
        ent = getattr(self.worldbrain, "entities", {}).get(h.entity_id)
        if ent is None:
            return
        try:
            attrs = dict(getattr(ent, "hypothesis_attributes", {}) or {})
            attrs.pop(h.attr_type, None)
            ent.hypothesis_attributes = attrs   # type: ignore[attr-defined]
            if h.attr_type == "name" and getattr(ent, "person_name", None) == h.value:
                ent.person_name = None          # type: ignore[attr-defined]
        except Exception:
            pass

    def _announce(self, h: Hypothesis) -> None:
        """NEVER silent: a discreet UIIntent announces the deduction (§17.2)."""
        if self._on_ui is None:
            return
        message = {
            "type": "ui_intent",
            "kind": "hypothesis_promoted",
            "entity_id": h.entity_id,
            "attr_type": h.attr_type,
            "value": h.value,
            "truth_level": "observed",
            "correctable": True,
            "text": f"J'ai déduit : c'est probablement {h.value} — corrige-moi si faux.",
            "evidence": [o.evidence_ref for o in h.occurrences if o.concordant and o.evidence_ref][:5],
            "as_of": _iso_now(),
        }
        try:
            self._on_ui(message)
        except Exception:
            pass

    # -------------------------------------------------------- clarification bridge
    def resolve_clarifications(self, *, entity_hint_by_name: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """Read the core's pending identity hypotheses and, where our signals have
        already converged (a promoted name), record a machine-sourced resolution
        SERVICE-SIDE with evidence (ADR §E38 — core untouched).

        ``clarification_reader`` (injectable, defaults to the core's
        ``list_clarifications``) provides the pending items. Returns the list of
        resolutions recorded this pass."""
        reader = self._clar_reader or self._default_clar_reader
        try:
            items = list(reader(person_id=self.person_id) or [])
        except Exception:
            return []
        entity_hint_by_name = {k.lower(): v for k, v in (entity_hint_by_name or {}).items()}
        promoted_names = {
            _norm_value(h.value).lower(): h
            for h in self.hypotheses.values()
            if h.attr_type == "name" and h.status == "promoted"
        }
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name = _norm_value(item.get("candidate_name") or item.get("display_name")
                               or item.get("name") or item.get("value")).lower()
            if not name or name not in promoted_names:
                continue
            h = promoted_names[name]
            core_id = str(item.get("hypothesis_id") or item.get("item_id") or item.get("source_id") or "")
            self._svc_db.execute(
                """INSERT INTO hypothesis_engine_resolutions(
                     person_id, core_hypothesis_id, entity_id, attr_type, value,
                     source, evidence_json, resolved_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.person_id, core_id, h.entity_id, h.attr_type, h.value,
                 "machine_convergence",
                 json.dumps([o.to_dict() for o in h.occurrences]), _iso_now()),
            )
            self._svc_db.commit()
            self.metrics["clarifications_resolved"] += 1
            out.append({
                "core_hypothesis_id": core_id, "entity_id": h.entity_id,
                "value": h.value, "source": "machine_convergence",
            })
        return out

    def _default_clar_reader(self, *, person_id: str) -> list[dict[str, Any]]:
        try:
            from mlomega_audio_elite.clarification_inbox_v14_8 import list_clarifications  # type: ignore
            res = list_clarifications(person_id=person_id, status="queued")
            return list(res.get("items") or [])
        except Exception:
            return []

    # -------------------------------------------------------- query
    def active_hypotheses(self, *, entity_id: str | None = None) -> list[dict[str, Any]]:
        out = [h.to_dict() for h in self.hypotheses.values()
               if h.status in ("hypothesis", "promoted")
               and (entity_id is None or h.entity_id == entity_id)]
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "hypotheses": [h.to_dict() for h in self.hypotheses.values()],
            "metrics": dict(self.metrics),
        }
