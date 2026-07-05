from __future__ import annotations

"""AttributeMemory — bi-modal attribute changes across sessions (E38 §2).

The world is not static: a price on a shelf, a note on a door, the haircut of a
known person — all of these are *attributes* that carry a value and can change
between one visit and the next. This module remembers observed attribute values
per (subject, attribute) and, on re-visit/re-encounter, surfaces a **change** when
the value differs — crossing the two modalities: something SEEN (OCR / VLM) can
contradict something HEARD, and vice-versa (the deliberately bi-modal check).

Everything is generic. There is NO "price" pattern, NO field list keyed to a
domain:

* the **subject** is any stable key — an entity_id, a person entity, a place/zone;
* the **attribute** is a free string key;
* the **value** is a free string;
* the **source** is ``ocr`` | ``vlm`` | ``heard`` — the modality that observed it.

Feeders (wired in the pipeline, all generic):

* **OCR ROI** (existing E29 path): text read on a crop, attached to the current
  place/zone as an ``ocr`` observation. The attribute key is derived generically
  from the reading (a labelled reading "clé: valeur" splits into key/value; an
  un-labelled reading is stored under a stable per-region key) — no domain lexicon.
* **VLM descriptions** (StrangerProfiler / what_is): the structured attributes it
  already returns are stored as ``vlm`` observations of the subject.
* **Heard facts**: a *generic* LLM extraction on final turns — "does this turn
  state an attribute/value fact about something present or about the place?" —
  strict JSON, NO "price" pattern. The frontier is a single injectable callable
  (mocked in tests at the real shape).

On a differing value for the same (subject, attribute) across sessions the module
asks the WorldBrain to emit a new ``attribute_changed`` ChangeEvent (before/after
+ the sources of BOTH sides), which WorldBrain persists into ``visual_events_v19``
and the scene adapter may surface proactively ("ceci a changé depuis la dernière
fois"). The *appearance of a known person* uses the SAME mechanism: a light VLM
appearance descriptor per encounter is stored as attribute observations, so an
inter-session diff of "hairstyle"/"clothing" is an ``attribute_changed`` like any
other — no special person path.
"""

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Generic LLM extraction: does this turn state an attribute/value fact about
# something present or the place? No "price" pattern — the model reads the NL.
_FACT_PROMPT_SYSTEM = (
    "You extract, from ONE conversation turn, whether it states a factual "
    "attribute and value about something physically present or about the current "
    "place. Answer strict JSON only with keys: states_fact (bool), "
    "subject_hint (string or null: what the fact is about), attribute (string or "
    "null: the property named), value (string or null), confidence (0..1). "
    "If the turn states no such attribute fact, states_fact=false."
)
_FACT_SCHEMA = {
    "states_fact": "bool",
    "subject_hint": "string|null",
    "attribute": "string|null",
    "value": "string|null",
    "confidence": "0..1",
}


@dataclass
class AttributeObservation:
    subject: str            # entity_id | person entity | place/zone key
    attribute: str          # free key
    value: str              # free value
    source: str             # ocr | vlm | heard
    session: str
    observed_at: str = field(default_factory=_iso_now)
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject, "attribute": self.attribute, "value": self.value,
            "source": self.source, "session": self.session,
            "observed_at": self.observed_at, "evidence_ref": self.evidence_ref,
        }


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _norm_value(s: Any) -> str:
    return " ".join(str(s or "").split()).strip()


class AttributeMemory:
    """Stores attribute observations and detects inter-session value changes."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        worldbrain: Any = None,
        llm: Any = None,
        service_db_path: str | Path | None = None,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        self.person_id = person_id
        self.worldbrain = worldbrain
        self.llm = llm
        self._now = now_fn or _iso_now
        self._svc_db = self._init_service_db(service_db_path)
        self.metrics = {
            "observations": 0,
            "attribute_changes": 0,
            "heard_facts": 0,
            "ocr_observations": 0,
            "vlm_observations": 0,
            "llm_unavailable": 0,
        }

    # -------------------------------------------------------- store
    def _init_service_db(self, path: str | Path | None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path) if path else ":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS attribute_memory_observations(
                 obs_id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT,
                 subject TEXT, attribute TEXT, value TEXT, source TEXT,
                 session TEXT, observed_at TEXT, evidence_ref TEXT)"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attr_subject ON attribute_memory_observations(person_id, subject, attribute, observed_at)"
        )
        conn.commit()
        return conn

    def _latest_prior(self, subject: str, attribute: str, session: str) -> AttributeObservation | None:
        """Most recent observation of this (subject, attribute) from ANOTHER session."""
        row = self._svc_db.execute(
            """SELECT * FROM attribute_memory_observations
               WHERE person_id=? AND subject=? AND attribute=? AND session<>?
               ORDER BY observed_at DESC, obs_id DESC LIMIT 1""",
            (self.person_id, subject, attribute, session),
        ).fetchone()
        if not row:
            return None
        return AttributeObservation(
            subject=row["subject"], attribute=row["attribute"], value=row["value"],
            source=row["source"], session=row["session"], observed_at=row["observed_at"],
            evidence_ref=row["evidence_ref"],
        )

    # -------------------------------------------------------- generic observe
    def observe(
        self,
        *,
        subject: str,
        attribute: str,
        value: str,
        source: str,
        session: str,
        evidence_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Record one attribute observation. If the same (subject, attribute) was
        seen with a DIFFERENT value in another session, emit an ``attribute_changed``
        ChangeEvent (bi-modal: carries the source of both sides). Returns the change
        dict when one fired, else None."""
        subject = _norm(subject)
        attribute = _norm(attribute)
        value = _norm_value(value)
        if not (subject and attribute and value):
            return None
        prior = self._latest_prior(subject, attribute, session)
        obs = AttributeObservation(
            subject=subject, attribute=attribute, value=value, source=source,
            session=session, observed_at=self._now(), evidence_ref=evidence_ref,
        )
        self._svc_db.execute(
            """INSERT INTO attribute_memory_observations(
                 person_id, subject, attribute, value, source, session, observed_at, evidence_ref)
               VALUES(?,?,?,?,?,?,?,?)""",
            (self.person_id, subject, attribute, value, source, session, obs.observed_at, evidence_ref),
        )
        self._svc_db.commit()
        self.metrics["observations"] += 1
        self.metrics[f"{source}_observations"] = self.metrics.get(f"{source}_observations", 0) + 1
        if prior is not None and _norm_value(prior.value).lower() != value.lower():
            return self._emit_change(prior, obs)
        return None

    def _emit_change(self, prior: AttributeObservation, cur: AttributeObservation) -> dict[str, Any]:
        """Ask the WorldBrain to record an ``attribute_changed`` ChangeEvent."""
        self.metrics["attribute_changes"] += 1
        change = None
        if self.worldbrain is not None and hasattr(self.worldbrain, "record_attribute_change"):
            try:
                change = self.worldbrain.record_attribute_change(
                    subject=cur.subject, attribute=cur.attribute,
                    before={"value": prior.value, "source": prior.source, "session": prior.session,
                            "observed_at": prior.observed_at},
                    after={"value": cur.value, "source": cur.source, "session": cur.session,
                           "observed_at": cur.observed_at},
                    evidence_refs=[r for r in (prior.evidence_ref, cur.evidence_ref) if r],
                )
            except Exception:
                change = None
        if change is None:
            change = {
                "type": "attribute_changed", "subject": cur.subject,
                "attribute": cur.attribute,
                "before": {"value": prior.value, "source": prior.source},
                "after": {"value": cur.value, "source": cur.source},
            }
        return change

    # -------------------------------------------------------- feeders
    def observe_ocr(
        self, *, subject: str, readings: Any, session: str, evidence_ref: str | None = None
    ) -> list[dict[str, Any]]:
        """Feed OCR ROI readings (list of {text,confidence,...} or plain strings).

        A labelled reading "clé: valeur" splits generically into (key, value); an
        un-labelled reading is stored under a stable region key so a later reading
        of the same region compares. No domain lexicon."""
        changes: list[dict[str, Any]] = []
        items = readings if isinstance(readings, (list, tuple)) else [readings]
        for idx, r in enumerate(items):
            text = _norm(r.get("text") if isinstance(r, Mapping) else r)
            if not text:
                continue
            attribute, value = self._split_labelled(text, idx)
            ch = self.observe(
                subject=subject, attribute=attribute, value=value, source="ocr",
                session=session, evidence_ref=evidence_ref,
            )
            if ch:
                changes.append(ch)
        return changes

    @staticmethod
    def _split_labelled(text: str, idx: int) -> tuple[str, str]:
        """Generic key/value split of a reading. "a: b" → (a, b); else (region#idx, text)."""
        for sep in (":", "：", "="):
            if sep in text:
                left, right = text.split(sep, 1)
                left, right = left.strip(), right.strip()
                if left and right:
                    return left.lower(), right
        return f"reading_region_{idx}", text

    def observe_vlm_attributes(
        self, *, subject: str, attributes: Mapping[str, Any], session: str, evidence_ref: str | None = None
    ) -> list[dict[str, Any]]:
        """Feed structured VLM attributes ({appearance, clothing, ...} or any keys)."""
        changes: list[dict[str, Any]] = []
        for k, v in (attributes or {}).items():
            value = _norm_value(v)
            if not value:
                continue
            ch = self.observe(
                subject=subject, attribute=_norm(k).lower(), value=value, source="vlm",
                session=session, evidence_ref=evidence_ref,
            )
            if ch:
                changes.append(ch)
        return changes

    def note_turn(
        self,
        text: str,
        *,
        session: str,
        subject_resolver: Callable[[str | None], str | None] | None = None,
        default_subject: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Extract a heard attribute/value fact (generic LLM) and observe it.

        ``subject_resolver`` maps the LLM's free ``subject_hint`` to a stable subject
        key (an entity_id / place / zone); when it returns None the ``default_subject``
        (usually the current place/zone) is used. Returns an ``attribute_changed``
        dict if the heard value differs from a prior seen/heard value, else None."""
        text = (text or "").strip()
        if not text or self.llm is None:
            return None
        try:
            data = self.llm.complete_json(_FACT_PROMPT_SYSTEM, text, schema_hint=_FACT_SCHEMA)
        except Exception:
            self.metrics["llm_unavailable"] += 1
            return None
        if not isinstance(data, Mapping) or not data.get("states_fact"):
            return None
        attribute = _norm(data.get("attribute"))
        value = _norm_value(data.get("value"))
        if not attribute or not value:
            return None
        self.metrics["heard_facts"] += 1
        subject = None
        if subject_resolver is not None:
            try:
                subject = subject_resolver(_norm(data.get("subject_hint")) or None)
            except Exception:
                subject = None
        subject = subject or default_subject
        if not subject:
            return None
        return self.observe(
            subject=subject, attribute=attribute.lower(), value=value, source="heard",
            session=session, evidence_ref=evidence_ref,
        )

    def observe_person_appearance(
        self, *, entity_id: str, descriptor: Mapping[str, Any], session: str, evidence_ref: str | None = None
    ) -> list[dict[str, Any]]:
        """A known person's light appearance descriptor per encounter → attribute
        observations, so an inter-session diff is an ``attribute_changed`` via the
        SAME mechanism (no special person path)."""
        return self.observe_vlm_attributes(
            subject=entity_id, attributes=descriptor, session=session, evidence_ref=evidence_ref,
        )

    def history(self, *, subject: str, attribute: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses = ["person_id=?", "subject=?"]
        params: list[Any] = [self.person_id, subject]
        if attribute:
            clauses.append("attribute=?")
            params.append(attribute)
        params.append(limit)
        rows = self._svc_db.execute(
            "SELECT * FROM attribute_memory_observations WHERE " + " AND ".join(clauses)
            + " ORDER BY observed_at DESC, obs_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
