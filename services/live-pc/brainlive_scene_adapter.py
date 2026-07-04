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
        self.metrics = {"hot_context_builds": 0, "deliveries_enqueued": 0}
        self._last_build_ts = 0.0

    # ----------------------------------------------------------------- context
    def set_active_task(self, task: Mapping[str, Any] | None) -> None:
        self._active_task = dict(task) if task else None

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
        self.metrics["hot_context_builds"] += 1
        return ctx

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

    def _enqueue(self, *, source_key: str, message: str, evidence_refs: Sequence[str], priority: float) -> dict[str, Any]:
        from mlomega_audio_elite import v18_delivery  # type: ignore

        candidate = {
            "message": message,
            "decision": "notify",
            "action_type": "context_card",
            "cooldown_key": source_key,
            "candidate_id": source_key,
            "priority": priority,
        }
        result = v18_delivery.enqueue_delivery(
            live_session_id=self.live_session_id,
            source_key=source_key,
            candidate={**candidate, "evidence_refs": list(evidence_refs)},
        )
        if result.get("status") == "queued":
            self.metrics["deliveries_enqueued"] += 1
        return result

    def evaluate_situations(self, ctx: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Detect §12.4 situations in the current scene and enqueue deliveries.

        Returns the enqueue results (one per fired situation). Idempotent within a
        session via ``source_key`` (also the dedup boundary of enqueue_delivery).
        """
        ctx = ctx or self.build_context()
        self._ensure_session()
        results: list[dict[str, Any]] = []
        evidence = list(ctx.get("evidence_refs") or [])

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

        return results
