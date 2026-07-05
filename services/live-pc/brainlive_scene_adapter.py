from __future__ import annotations

"""BrainLiveSceneAdapter — closes the live loop (E28).

Periodically (and on significant change) it builds a compact
:class:`HotSceneContext` from the WorldBrain present — session/place/map_quality
+ focus + important visible entities + activity + active translation + relevant
changes/last-seen + aggregated ReflexEvents + evidence refs + omissions — under a
**hard character budget** (the spirit of §2.4: everything is measured and the
overflow is dropped into ``omissions``, never silently truncated).

It then closes the loop two ways:

1. It publishes the current ``world_state`` into the REAL core tables via
   ``v19_visual_context.publish_visual_context`` (which the ``v18_context``
   wrapper picks up automatically).
2. When a §12.4 situation justifies an intervention (a *known person* in scene, a
   *lost object found again*, an *active task*), it builds a candidate and calls
   **``v18_delivery.enqueue_delivery`` directly** with a meaningful ``source_key``
   (scene+subject), ``decision='notify'`` and evidence refs. The E6
   delivery_adapter carries it the rest of the way to the glasses.

ADR (docs/DECISIONS.md §E28): we deliberately take the *direct enqueue* path
rather than re-entering ``v18_8_live_policy``/``brainlive_hotloop``. Rationale:
the hot-loop entry expects a full episode/manifest/fused/route bundle produced by
the offline assembly chain; a live scene has none of that. ``enqueue_delivery``
is the single documented H1 delivery primitive (handoff §8.1), it owns dedup +
cooldown, and it is the reversible choice — a later step can swap in the hot-loop
without changing the queue contract.
"""

import importlib.util
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


# --------------------------------------------------------------------------- config
@dataclass
class SceneAdapterConfig:
    hot_budget_chars: int = 4000        # hard budget for the HotSceneContext
    min_build_interval_s: float = 2.0   # cadence between periodic builds
    person_conf_threshold: float = 0.55  # below identity threshold → no name (§17.2)
    max_visible_entities: int = 8
    max_changes: int = 6


# --------------------------------------------------------------------------- builder
def build_hot_scene_context(
    *,
    session_id: str,
    world: Mapping[str, Any],
    focus: Mapping[str, Any] | None = None,
    translation_active: Mapping[str, Any] | None = None,
    people_identified: Sequence[Mapping[str, Any]] | None = None,
    activity: Mapping[str, Any] | None = None,
    reflex_events: Sequence[Mapping[str, Any]] | None = None,
    brain2_memory: Sequence[Mapping[str, Any]] | None = None,
    config: SceneAdapterConfig | None = None,
) -> dict[str, Any]:
    """Assemble a budget-bounded HotSceneContext dict (contract-shaped).

    ``world`` is a :meth:`WorldBrain.snapshot` output. Fields are appended in
    priority order and anything that would push the serialised size over
    ``hot_budget_chars`` is dropped into ``omissions`` (traceable, §2.4).
    """
    import json

    cfg = config or SceneAdapterConfig()
    entities = list(world.get("entities") or [])
    changes = list(world.get("recent_changes") or [])

    # visible entities first, most-recent last-seen first
    visible = sorted(
        (e for e in entities if e.get("lifecycle") == "confirmed"),
        key=lambda e: e.get("age_seconds", 1e9),
    )[: cfg.max_visible_entities]
    last_seen = sorted(
        (e for e in entities if e.get("lifecycle") == "last_seen"),
        key=lambda e: e.get("age_seconds", 1e9),
    )

    ctx: dict[str, Any] = {
        "contracts_version": "v19.0",
        "session_id": session_id,
        "as_of": _iso_now(),
        "place": {"place_hint": world.get("place_hint"), "active_zone": world.get("active_zone")},
        "map_quality": float(world.get("map_quality") or 0.0),
        "visible_entities": [],
        "people_identified": [],
        "changes": [],
        "reflex_events": [],
        "brain2_memory": [],
        "evidence_refs": [],
        "omissions": [],
    }
    if focus:
        ctx["focus"] = dict(focus)
    if activity:
        ctx["activity"] = dict(activity)
    if translation_active:
        ctx["translation_active"] = dict(translation_active)

    omitted_count = [0]

    def _omit(ref: str) -> None:
        # Keep the omission log compact: a bounded sample + a running count so a
        # flood of dropped fields cannot itself blow the budget.
        omitted_count[0] += 1
        if len(ctx["omissions"]) < 5:
            ctx["omissions"].append(ref)

    def _size(obj: Any) -> int:
        return len(json.dumps(obj, default=str))

    # Ordered fields with the section that receives them if they fit.
    ordered: list[tuple[str, list[Any]]] = [
        ("visible_entities", [{"entity_id": e.get("entity_id"), "label": e.get("label"),
                                "confidence": e.get("confidence"), "age_seconds": e.get("age_seconds")}
                               for e in visible]),
        ("people_identified", [dict(p) for p in (people_identified or [])]),
        ("changes", [c for c in changes][: cfg.max_changes]),
        ("reflex_events", [dict(r) for r in (reflex_events or [])]),
        ("brain2_memory", [dict(m) for m in (brain2_memory or [])]),
    ]

    evidence: set[str] = set()
    for e in visible + last_seen:
        for r in e.get("evidence") or []:
            evidence.add(r)

    for field_name, items in ordered:
        for item in items:
            trial = dict(ctx)
            trial[field_name] = ctx[field_name] + [item]
            if _size(trial) + _size(sorted(evidence)) <= cfg.hot_budget_chars:
                ctx[field_name].append(item)
            else:
                _omit(f"{field_name}:{item.get('entity_id') or item.get('type') or item.get('skill') or 'item'}")

    # last-seen entities are appended into changes-adjacent omissions awareness
    for e in last_seen:
        marker = {"entity_id": e.get("entity_id"), "label": e.get("label"),
                  "state": "last_seen", "age_seconds": e.get("age_seconds")}
        trial = dict(ctx)
        trial["changes"] = ctx["changes"] + [marker]
        if _size(trial) + _size(sorted(evidence)) <= cfg.hot_budget_chars:
            ctx["changes"].append(marker)
        else:
            _omit(f"last_seen:{e.get('entity_id')}")

    # Evidence refs last, trimming to fit.
    ev_sorted = sorted(evidence)
    while ev_sorted and _size({**ctx, "evidence_refs": ev_sorted}) > cfg.hot_budget_chars:
        dropped = ev_sorted.pop()
        _omit(f"evidence:{dropped}")
    ctx["evidence_refs"] = ev_sorted
    if omitted_count[0] > len(ctx["omissions"]):
        ctx["omissions"].append(f"+{omitted_count[0] - len(ctx['omissions'])}_more")
    return ctx


# --------------------------------------------------------------------------- adapter
class BrainLiveSceneAdapter:
    """Drives world_state publication + situation-triggered delivery (§12.4)."""

    def __init__(
        self,
        *,
        person_id: str,
        live_session_id: str,
        worldbrain: Any,
        config: SceneAdapterConfig | None = None,
        db_path: Any = None,
        known_people: Mapping[str, Mapping[str, Any]] | None = None,
        proactive: Any = None,
        predictive_retrieval: Any = None,
        on_entity_hot_update: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.person_id = person_id
        self.live_session_id = live_session_id
        self.world = worldbrain
        self.config = config or SceneAdapterConfig()
        self.db_path = db_path
        self.known_people = dict(known_people or {})  # label → {name, relation, ...}
        self._active_task: dict[str, Any] | None = None
        self._transcript_hint: str | None = None
        self._delivered_sources: set[str] = set()
        # E34: nightly engines brought into the live loop + dense retrieval +
        # device prefetch. All optional — a bare adapter keeps its E28 behaviour.
        self.proactive = proactive
        self.predictive_retrieval = predictive_retrieval
        self._on_entity_hot_update = on_entity_hot_update
        self._prefetched_people: set[str] = set()
        # E35 §4: generalised hot-context dedup sets (one push per subject/session).
        self._pushed_objects: set[str] = set()
        self._pushed_zones: set[str] = set()
        self._pushed_tasks: set[str] = set()
        self.metrics = {
            "hot_context_builds": 0,
            "deliveries_enqueued": 0,
            "proactive_predictions": 0,
            "proactive_interventions": 0,
            "clarifications_asked": 0,
            "similar_experiences": 0,
            "entity_hot_updates": 0,
            "spatial_hot_updates": 0,
            "object_hot_updates": 0,
            "task_hot_updates": 0,
        }
        self._last_build_ts = 0.0

    # ----------------------------------------------------------------- context
    def set_active_task(self, task: Mapping[str, Any] | None) -> None:
        self._active_task = dict(task) if task else None
        # E35 §4c: a task starting / advancing → push task_hot to the device.
        if self._active_task:
            self.push_task_hot(self._active_task)

    def note_transcript(self, text: str) -> None:
        """AudioRT transcript → conversation context for situation detection."""
        self._transcript_hint = (text or "").strip() or None

    # ----------------------------------------------------------------- build
    def build_context(
        self,
        *,
        focus: Mapping[str, Any] | None = None,
        translation_active: Mapping[str, Any] | None = None,
        reflex_events: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.world.snapshot()
        people = self._identify_people(snapshot)
        activity = {"transcript_hint": self._transcript_hint} if self._transcript_hint else None
        ctx = build_hot_scene_context(
            session_id=self.live_session_id,
            world=snapshot,
            focus=focus,
            translation_active=translation_active,
            people_identified=people,
            activity=activity,
            reflex_events=reflex_events,
            config=self.config,
        )
        # E34 §2: fold the nightly engines' open items into the hot context so the
        # policy LLM can reference them. Compact, and dropped if it would overflow.
        if self.proactive is not None:
            try:
                snap = self.proactive.snapshot()
                if any(snap.get(k) for k in ("predictions", "interventions", "clarifications")):
                    self._fold_section(ctx, "proactive", snap)
            except Exception:
                pass
        # E34 §3: dense predictive retrieval — "experiences similaires" from past
        # cases matching the current subject/entities. Clean degrade if Qdrant off.
        similar = self._retrieve_similar(ctx)
        if similar:
            self._fold_section(ctx, "similar_experiences", similar)
            self.metrics["similar_experiences"] += len(similar)
        self.metrics["hot_context_builds"] += 1
        return ctx

    def _fold_section(self, ctx: dict[str, Any], key: str, value: Any) -> None:
        """Add a section to the hot context only if it stays within the budget."""
        import json

        trial = {**ctx, key: value}
        if len(json.dumps(trial, default=str)) <= self.config.hot_budget_chars:
            ctx[key] = value
        else:
            ctx.setdefault("omissions", []).append(key)

    def _retrieve_similar(self, ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Dense predictive retrieval on the current context (E34 §3).

        Wraps ``get_predictive_backend().retrieve(...)``. The retrieval frontier
        (Qdrant + reranker) may be down; any failure yields an empty list and a
        one-line WARN, never a crash (honest degrade)."""
        if self.predictive_retrieval is None:
            return []
        query_text = self._subject_text(ctx)
        if not query_text:
            return []
        try:
            cands = self.predictive_retrieval.retrieve_for_live(
                person_id=self.person_id,
                query_text=query_text,
                session_id=self.live_session_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            import sys as _sys

            print(f"[scene_adapter] predictive retrieval unavailable: {str(exc)[:120]}", file=_sys.stderr)
            return []
        out: list[dict[str, Any]] = []
        for c in (cands or [])[:3]:
            if isinstance(c, Mapping):
                out.append({"text": str(c.get("text") or "")[:160], "score": c.get("score")})
        return out

    @staticmethod
    def _subject_text(ctx: Mapping[str, Any]) -> str:
        parts: list[str] = []
        act = ctx.get("activity")
        if isinstance(act, Mapping) and act.get("transcript_hint"):
            parts.append(str(act.get("transcript_hint")))
        parts.extend(str(e.get("label") or "") for e in (ctx.get("visible_entities") or [])[:5])
        return " ".join(p for p in parts if p).strip()

    def _identify_people(self, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in snapshot.get("entities") or []:
            if e.get("label") != "person":
                continue
            conf = float(e.get("confidence") or 0.0)
            known = self.known_people.get(e.get("entity_id")) or None
            item = {"entity_id": e.get("entity_id"), "confidence": e.get("confidence")}
            # §17.2: no name below the identity threshold.
            if known and conf >= self.config.person_conf_threshold:
                item.update({"name": known.get("name"), "relation": known.get("relation"), "identified": True})
            else:
                item["identified"] = False
            out.append(item)
        return out

    # ----------------------------------------------------------------- deliver
    def _ensure_session(self) -> None:
        """Ensure a brainlive_sessions row exists so enqueue_delivery resolves owner."""
        from mlomega_audio_elite import v19_visual_context as ctx  # type: ignore

        ctx.publish_visual_context(
            person_id=self.person_id, live_session_id=self.live_session_id,
            world_state=None, observations=None, db_path=self.db_path,
        )

    def _enqueue(self, *, source_key: str, message: str, evidence_refs: Sequence[str], priority: float,
                 kind: str | None = None, item_id: str | None = None) -> dict[str, Any]:
        from mlomega_audio_elite import v18_delivery  # type: ignore

        candidate = {
            "message": message,
            "decision": "notify",
            "action_type": "context_card",
            "cooldown_key": source_key,
            "candidate_id": source_key,
            "priority": priority,
        }
        # A clarification question carries its inbox item_id so the device can post
        # the user's spoken answer back through the existing conversation path.
        if kind:
            candidate["kind"] = kind
        if item_id:
            candidate["clarification_item_id"] = item_id
        result = v18_delivery.enqueue_delivery(
            live_session_id=self.live_session_id,
            source_key=source_key,
            candidate={**candidate, "evidence_refs": list(evidence_refs)},
        )
        if result.get("status") == "queued":
            self.metrics["deliveries_enqueued"] += 1
        return result

    # ------------------------------------------------------------ prefetch (§5)
    def prefetch_relation_pack(self, *, entity_id: str | None, person_id: str | None, name: str | None) -> dict[str, Any] | None:
        """Push a compact relation pack for a just-identified person to the device
        (E34 §5). The device's SceneCache.entities_hot integrates it so the
        ContextCard renders from the local cache with zero round-trip.

        The relation pack is what ``build_active_context`` already assembles as
        ``active_relationship_packs`` (last subjects / promises from the core
        relationship tables) — we only read it and compact it. Emitted once per
        (entity, person) per session; deduped by ``_prefetched_people``."""
        if self._on_entity_hot_update is None or not person_id:
            return None
        dedup = f"{entity_id or ''}:{person_id}"
        if dedup in self._prefetched_people:
            return None
        self._prefetched_people.add(dedup)
        pack = self._relation_pack(person_id)
        message = {
            "type": "entity_hot_update",
            "entity_id": entity_id,
            "person_id": person_id,
            "name": name,
            "relation_pack": pack,
            "as_of": _iso_now(),
        }
        try:
            self._on_entity_hot_update(message)
            self.metrics["entity_hot_updates"] += 1
        except Exception:
            return None
        return message

    # ------------------------------------------------ generalised hot (E35 §4)
    def _emit_hot(self, message: dict[str, Any], metric: str) -> dict[str, Any] | None:
        """Push a hot-update message to the device SceneCache (same DataChannel as
        the E34 entity prefetch). Bounded by a per-message budget; a message that
        would exceed it is not sent (never an unbounded push)."""
        import json

        if self._on_entity_hot_update is None:
            return None
        if len(json.dumps(message, default=str)) > self.config.hot_budget_chars:
            return None
        try:
            self._on_entity_hot_update(message)
        except Exception:
            return None
        self.metrics[metric] = self.metrics.get(metric, 0) + 1
        return message

    def push_spatial_hot(self, *, snapshot: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """(a) When a session zone / place is recognised, push a ``spatial_hot_update``
        to SceneCache.spatial_hot: the active zone, measured map_quality, a few
        useful last-seens *of that zone*, and any daily routine that matches here
        ("ici, d'habitude tu…"). One push per zone per session (dedup)."""
        snap = snapshot or self.world.snapshot()
        zone = snap.get("active_zone") or snap.get("place_hint")
        if not zone:
            return None
        key = str(zone)
        if key in self._pushed_zones:
            return None
        self._pushed_zones.add(key)
        # a few useful last-seen entities (phone/keys-class) tied to this session
        last_seens = [
            {"entity_id": e.get("entity_id"), "label": e.get("label"),
             "age_seconds": e.get("age_seconds"), "lifecycle": e.get("lifecycle")}
            for e in (snap.get("entities") or [])
            if e.get("lifecycle") in ("last_seen", "confirmed")
        ][:4]
        message = {
            "type": "spatial_hot_update",
            "zone": key,
            "place_hint": snap.get("place_hint"),
            "map_quality": snap.get("map_quality"),
            "last_seens": last_seens,
            "routines": self._matching_routines(place_key=key),
            "as_of": _iso_now(),
        }
        return self._emit_hot(message, "spatial_hot_updates")

    def push_object_hot(self, entity: Mapping[str, Any]) -> dict[str, Any] | None:
        """(b) A durable object promoted / found again → ``entity_hot_update`` with
        ``kind='object'`` (last_seen, relations). Generalises the E34 person path.
        One push per object per session."""
        eid = str(entity.get("entity_id") or "")
        if not eid or eid in self._pushed_objects:
            return None
        self._pushed_objects.add(eid)
        message = {
            "type": "entity_hot_update",
            "kind": "object",
            "entity_id": eid,
            "label": entity.get("label"),
            "last_seen": entity.get("last_seen"),
            "confidence": entity.get("confidence"),
            "relations": self._entity_relations(eid),
            "as_of": _iso_now(),
        }
        return self._emit_hot(message, "object_hot_updates")

    def push_task_hot(self, task: Mapping[str, Any]) -> dict[str, Any] | None:
        """(c) An active task / situation started → ``task_hot_update`` to
        SceneCache.task_hot: goal, current step, tools. One push per task per
        session (re-pushed only when the step changes)."""
        task_key = str(task.get("task_key") or task.get("goal") or "active")
        step = str(task.get("next_step") or task.get("step") or "")
        dedup = f"{task_key}:{step}"
        if dedup in self._pushed_tasks:
            return None
        self._pushed_tasks.add(dedup)
        message = {
            "type": "task_hot_update",
            "task_key": task_key,
            "goal": task.get("goal") or task.get("title"),
            "step": step or None,
            "tools": list(task.get("tools") or [])[:6],
            "as_of": _iso_now(),
        }
        return self._emit_hot(message, "task_hot_updates")

    def _entity_relations(self, entity_id: str) -> list[dict[str, Any]]:
        """Current-frame relations involving this entity (compact)."""
        try:
            rels = self.world.snapshot().get("relations") or []
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for r in rels:
            if entity_id in (r.get("subject"), r.get("object")):
                out.append({k: r.get(k) for k in ("subject", "predicate", "object")})
        return out[:4]

    def _matching_routines(self, *, place_key: str) -> list[dict[str, Any]]:
        """(d) Daily routines of ``brain2_spatial_routine_models`` that match the
        current place → included in the spatial hot pack ("ici, d'habitude tu…").
        Best-effort; a cold/absent table yields []."""
        try:
            from mlomega_audio_elite.db import connect  # type: ignore
        except Exception:
            return []
        pk = (place_key or "").lower()
        try:
            with connect(self.db_path) as con:
                rows = [dict(r) for r in con.execute(
                    """SELECT entity_key, place_key, time_slot, occurrence_count, confidence
                       FROM brain2_spatial_routine_models
                       WHERE person_id=? ORDER BY confidence DESC, occurrence_count DESC LIMIT 20""",
                    (self.person_id,),
                ).fetchall()]
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            rp = str(r.get("place_key") or "").lower()
            # match on place equality or containment either way (zone ids vs hints)
            if rp and (rp == pk or rp in pk or pk in rp):
                out.append({
                    "entity_key": r.get("entity_key"), "place_key": r.get("place_key"),
                    "time_slot": r.get("time_slot"), "confidence": r.get("confidence"),
                })
            if len(out) >= 3:
                break
        return out

    def _relation_pack(self, person_id: str) -> list[dict[str, Any]]:
        """Compact relation pack (last topics / promises) for a person from the
        core relationship tables via ``build_active_context``. Best-effort."""
        try:
            from mlomega_audio_elite import brainlive_v15  # type: ignore

            ac = brainlive_v15.build_active_context(self.live_session_id, active_people=[person_id], limit=5)
            b2 = ac.get("brain2_context") if isinstance(ac, Mapping) else None
            packs = (b2 or {}).get("active_relationship_packs") if isinstance(b2, Mapping) else None
            out: list[dict[str, Any]] = []
            for p in (packs or [])[:4]:
                if not isinstance(p, Mapping):
                    continue
                out.append({k: p.get(k) for k in ("known_person_id", "person_hint", "summary",
                                                   "last_topics", "open_promises", "relationship_type")
                            if p.get(k) is not None})
            return out
        except Exception:
            return []

    def evaluate_situations(self, ctx: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Detect §12.4 situations in the current scene and enqueue deliveries.

        Returns the enqueue results (one per fired situation). Idempotent within a
        session via ``source_key`` (also the dedup boundary of enqueue_delivery).
        """
        ctx = ctx or self.build_context()
        self._ensure_session()
        results: list[dict[str, Any]] = []
        evidence = list(ctx.get("evidence_refs") or [])

        # E35 §4a/§4b: generalise the hot-context pushes. A recognised session zone
        # → spatial_hot (+ matching daily routines). Durable/reappeared objects →
        # entity_hot_update kind=object. Both deduped per subject per session.
        snap = self.world.snapshot()
        self.push_spatial_hot(snapshot=snap)
        for e in snap.get("entities") or []:
            if e.get("lifecycle") in ("confirmed", "last_seen") and e.get("label") != "person":
                self.push_object_hot(e)

        # (1) Known person in scene → ContextCard.
        for p in ctx.get("people_identified") or []:
            if p.get("identified") and p.get("name"):
                key = f"scene:{self.live_session_id}:person:{p.get('entity_id')}"
                msg = f"{p['name']} est là" + (f" ({p['relation']})" if p.get("relation") else "")
                results.append(self._enqueue(source_key=key, message=msg, evidence_refs=evidence, priority=0.6))

        # (2) Lost object found again → last_seen entity that reappeared (moved/appeared change).
        for c in ctx.get("changes") or []:
            if c.get("type") == "appeared":
                key = f"scene:{self.live_session_id}:found:{c.get('entity_id')}"
                msg = f"{c.get('label')} de nouveau visible"
                ev = list(c.get("evidence") or evidence)
                results.append(self._enqueue(source_key=key, message=msg, evidence_refs=ev, priority=0.5))

        # (3) Active task → TaskCard (one action).
        if self._active_task:
            key = f"scene:{self.live_session_id}:task:{self._active_task.get('task_key') or 'active'}"
            step = self._active_task.get("next_step") or self._active_task.get("step") or "prochaine étape"
            results.append(self._enqueue(source_key=key, message=str(step), evidence_refs=evidence, priority=0.55))

        # E34 proactive situations (only when the nightly engines are wired) -----
        if self.proactive is not None:
            results.extend(self._evaluate_proactive(ctx, evidence))

        return results

    def _evaluate_proactive(self, ctx: Mapping[str, Any], evidence: Sequence[str]) -> list[dict[str, Any]]:
        """§2 proactive situations: a day-prediction matches the scene; a nightly
        intervention is relevant; a clarification is due in a calm context."""
        results: list[dict[str, Any]] = []
        conversation_active = bool((ctx.get("activity") or {}).get("transcript_hint")) if isinstance(ctx.get("activity"), Mapping) else False

        # (a) A prediction of the day matches the scene/conversation (same specs as
        # the outcome watcher) → "tu voulais racheter X".
        try:
            for p in self.proactive.match_predictions(ctx):
                pid = p.get("prediction_id") or "pred"
                key = f"scene:{self.live_session_id}:prediction:{pid}"
                msg = str(p.get("statement") or "Rappel du jour")
                ev = list(p.get("evidence_refs") or evidence)
                results.append(self._enqueue(source_key=key, message=msg, evidence_refs=ev, priority=0.65))
                self.metrics["proactive_predictions"] += 1
        except Exception:
            pass

        # (b) A nightly intervention relevant to the current context → delivery.
        try:
            for i in self.proactive.relevant_interventions(ctx):
                qid = i.get("queue_id") or "interv"
                key = f"scene:{self.live_session_id}:intervention:{qid}"
                msg = str(i.get("message") or i.get("title") or "")
                if not msg:
                    continue
                results.append(self._enqueue(source_key=key, message=msg, evidence_refs=list(evidence), priority=0.6))
                self.metrics["proactive_interventions"] += 1
        except Exception:
            pass

        # (c) A clarification question asked at the right (calm) moment → a question
        # ContextCard. The user's spoken answer travels back on the existing
        # conversation path (ConversationBridge → clarification inbox nightly).
        try:
            clar = self.proactive.due_clarification(ctx, conversation_active=conversation_active)
            if clar is not None:
                item_id = clar.get("item_id") or "clar"
                question = str(clar.get("question_text") or clar.get("question") or "")
                if question:
                    key = f"scene:{self.live_session_id}:clarification:{item_id}"
                    res = self._enqueue(
                        source_key=key,
                        message=question,
                        evidence_refs=list(evidence),
                        priority=0.5,
                        kind="clarification_question",
                        item_id=str(item_id),
                    )
                    results.append(res)
                    if res.get("status") == "queued":
                        self.metrics["clarifications_asked"] += 1
                        self.proactive.mark_clarification_delivered(str(item_id))
        except Exception:
            pass

        return results
