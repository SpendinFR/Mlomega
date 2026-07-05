from __future__ import annotations

"""RoutineAssociations — learned routine→object associations (E38 §3).

Some objects belong to a routine: when you approach the place/entity where a
routine happens, the object the routine needs is worth surfacing before you look
for it. E35 already pushes a zone's routines and last-seens; E38 adds the LEARNED
link between a routine (or the entity/zone it lives at) and the objects that
co-occur with it — so approaching that zone/entity proactively raises the
last-seen of the associated object.

The association is **learned from data**, never a hardcoded pair. For each routine
model (``brain2_spatial_routine_models``: entity_key / place_key / time_slot) we
count how often each object entity was seen in the SAME place/time window from the
visual last-seen stream (``visual_events_v19`` entity_last_seen events). The
co-occurrence count, normalised by the object's overall frequency, yields an
association score. In live, when the wearer approaches a zone/entity whose
associated object scores above ``min_score``, the object's last-seen is pushed via
the existing ``push_object_hot`` (E35), plus a discreet suggestion when the object
is NOT currently visible ("ta <objet> est d'habitude ici").

No object/routine pair is written in code — the scoring is purely a count over the
stored data, and the tests seed arbitrary, varied keys to prove genericity.
"""

import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_connect():
    from mlomega_audio_elite.db import connect  # type: ignore

    return connect


@dataclass
class AssociationConfig:
    min_score: float = 0.3       # push threshold for an association
    min_cooccurrence: int = 2    # a link needs at least this many co-sightings
    max_pushes_per_place: int = 3


@dataclass
class Association:
    place_key: str
    object_label: str
    object_entity_id: str | None
    cooccurrence: int
    score: float
    routine_entity_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "place_key": self.place_key,
            "object_label": self.object_label,
            "object_entity_id": self.object_entity_id,
            "cooccurrence": self.cooccurrence,
            "score": round(self.score, 4),
            "routine_entity_key": self.routine_entity_key,
        }


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


class RoutineAssociations:
    """Learns routine→object co-occurrences and pushes them proactively in live."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        db_path: Any = None,
        worldbrain: Any = None,
        scene_adapter: Any = None,
        config: AssociationConfig | None = None,
    ) -> None:
        self.person_id = person_id
        self.db_path = db_path
        self.worldbrain = worldbrain
        self.scene_adapter = scene_adapter
        self.config = config or AssociationConfig()
        # place_key -> list[Association] (sorted by score desc)
        self.associations: dict[str, list[Association]] = {}
        self._pushed: set[str] = set()   # dedup "place|object" per session
        self.metrics = {
            "associations_learned": 0,
            "routine_pushes": 0,
            "suggestions": 0,
        }

    # -------------------------------------------------------- learn
    def learn(self) -> dict[str, list[Association]]:
        """Learn associations from the stored routine models + visual last-seens.

        Counts object last-seens whose place matches each routine's place (place
        equality or containment either way — zone ids vs place hints). Score =
        cooccurrence / (object total sightings) — a common object shared across
        many places scores lower than one specific to this place."""
        connect = _load_connect()
        routines: list[dict[str, Any]] = []
        last_seens: list[dict[str, Any]] = []
        try:
            with connect(self.db_path) as con:
                try:
                    routines = [dict(r) for r in con.execute(
                        """SELECT entity_key, place_key, time_slot, occurrence_count, confidence
                           FROM brain2_spatial_routine_models WHERE person_id=?""",
                        (self.person_id,),
                    ).fetchall()]
                except Exception:
                    routines = []
                try:
                    rows = con.execute(
                        """SELECT entity_json, place_json, observation_json FROM visual_events_v19
                           WHERE person_id=? AND event_type='entity_last_seen'""",
                        (self.person_id,),
                    ).fetchall()
                    for r in rows:
                        ent = json.loads(r["entity_json"] or "{}")
                        place = json.loads(r["place_json"] or "{}")
                        last_seens.append({"entity": ent, "place": place})
                except Exception:
                    last_seens = []
        except Exception:
            return {}

        # object total sightings (by label) for normalisation
        obj_total: dict[str, int] = defaultdict(int)
        for ls in last_seens:
            ent = ls.get("entity") or {}
            if str(ent.get("kind") or "") == "person" or str(ent.get("label") or "") == "person":
                continue
            label = _norm(ent.get("label"))
            if label:
                obj_total[label] += 1

        cfg = self.config
        learned: dict[str, list[Association]] = {}
        for routine in routines:
            place = _norm(routine.get("place_key"))
            if not place:
                continue
            # count co-occurring objects at this place
            counts: dict[str, dict[str, Any]] = {}
            for ls in last_seens:
                ent = ls.get("entity") or {}
                place_j = ls.get("place") or {}
                if str(ent.get("kind") or "") == "person" or str(ent.get("label") or "") == "person":
                    continue
                ls_place = _norm(place_j.get("place_key") or place_j.get("active_zone")
                                 or place_j.get("place_hint") or place_j.get("zone"))
                if not ls_place:
                    continue
                if not (ls_place == place or ls_place in place or place in ls_place):
                    continue
                label = _norm(ent.get("label"))
                if not label:
                    continue
                slot = counts.setdefault(label, {"count": 0, "entity_id": ent.get("entity_id")})
                slot["count"] += 1
                if ent.get("entity_id"):
                    slot["entity_id"] = ent.get("entity_id")
            assocs: list[Association] = []
            for label, slot in counts.items():
                co = int(slot["count"])
                if co < cfg.min_cooccurrence:
                    continue
                total = max(1, obj_total.get(label, co))
                score = co / total
                assocs.append(Association(
                    place_key=routine.get("place_key"), object_label=label,
                    object_entity_id=slot.get("entity_id"), cooccurrence=co,
                    score=score, routine_entity_key=routine.get("entity_key"),
                ))
            if assocs:
                assocs.sort(key=lambda a: (a.score, a.cooccurrence), reverse=True)
                learned[place] = assocs
                self.metrics["associations_learned"] += len(assocs)
        self.associations = learned
        return learned

    # -------------------------------------------------------- live
    def _match_place(self, place_key: str) -> list[Association]:
        pk = _norm(place_key)
        if not pk:
            return []
        if pk in self.associations:
            return self.associations[pk]
        for k, assocs in self.associations.items():
            if k in pk or pk in k:
                return assocs
        return []

    def on_approach(
        self,
        *,
        place_key: str | None = None,
        entity_key: str | None = None,
        visible_labels: Any = None,
    ) -> list[dict[str, Any]]:
        """The wearer approaches a zone/entity: push the last-seen of each
        associated object above ``min_score`` (via ``push_object_hot``), plus a
        discreet suggestion when the object is not currently visible.

        Returns the list of pushes performed. Deduped per (place, object) per
        session. ``entity_key`` also matches a routine keyed to that entity."""
        place_key = place_key or ""
        assocs = self._match_place(place_key)
        if not assocs and entity_key:
            ek = _norm(entity_key)
            assocs = [a for lst in self.associations.values() for a in lst
                      if _norm(a.routine_entity_key) == ek]
            assocs.sort(key=lambda a: (a.score, a.cooccurrence), reverse=True)
        visible = {_norm(v) for v in (visible_labels or [])}
        cfg = self.config
        pushes: list[dict[str, Any]] = []
        for a in assocs[: cfg.max_pushes_per_place]:
            if a.score < cfg.min_score:
                continue
            dedup = f"{_norm(a.place_key)}|{a.object_label}"
            if dedup in self._pushed:
                continue
            self._pushed.add(dedup)
            entity = self._object_entity(a)
            if self.scene_adapter is not None and hasattr(self.scene_adapter, "push_object_hot"):
                try:
                    self.scene_adapter.push_object_hot(entity)
                    self.metrics["routine_pushes"] += 1
                except Exception:
                    pass
            not_visible = a.object_label not in visible
            if not_visible:
                self.metrics["suggestions"] += 1
                self._suggest(a)
            pushes.append({**a.to_dict(), "object_visible": not not_visible})
        return pushes

    def _object_entity(self, a: Association) -> dict[str, Any]:
        """Best last-seen entity dict for the association's object (for the push)."""
        if a.object_entity_id and self.worldbrain is not None:
            ent = getattr(self.worldbrain, "entities", {}).get(a.object_entity_id)
            if ent is not None and hasattr(ent, "to_dict"):
                try:
                    return ent.to_dict()
                except Exception:
                    pass
        if self.worldbrain is not None and hasattr(self.worldbrain, "find_entity"):
            try:
                ent = self.worldbrain.find_entity(a.object_label)
                if ent is not None:
                    return ent.to_dict()
            except Exception:
                pass
        return {
            "entity_id": a.object_entity_id or f"assoc:{a.object_label}",
            "label": a.object_label, "last_seen": None,
        }

    def _suggest(self, a: Association) -> None:
        """Discreet suggestion when the associated object is not visible."""
        adapter = self.scene_adapter
        emit = getattr(adapter, "_on_entity_hot_update", None) if adapter is not None else None
        if not callable(emit):
            return
        try:
            emit({
                "type": "ui_intent",
                "kind": "routine_object_suggestion",
                "place_key": a.place_key,
                "object_label": a.object_label,
                "object_entity_id": a.object_entity_id,
                "score": round(a.score, 3),
                "text": f"Ta {a.object_label} est d'habitude ici.",
                "as_of": _iso_now(),
            })
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "associations": {k: [a.to_dict() for a in v] for k, v in self.associations.items()},
            "metrics": dict(self.metrics),
        }
