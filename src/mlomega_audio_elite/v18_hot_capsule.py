from __future__ import annotations

"""Bounded, immutable prompt rendering for the V18 hot path.

The context gateway can retain a rich provenance manifest.  The live model must
never receive that archive wholesale: this renderer produces the *exact* JSON
payload sent to the model under a hard character budget.  It only removes whole
fields/references and records omissions; it never slices a JSON document.
"""

from typing import Any, Mapping

from .utils import json_dumps


VERSION = "18.4.0-hot-capsule"


def _limit(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    import os
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def hot_input_budget(manifest: Mapping[str, Any] | None) -> int:
    """Return a small but usable hard live-input budget.

    A historical manifest may have been built with a larger retrieval budget.
    That is an index budget, not permission to copy a long conversation into
    the hot prompt.
    """
    requested = 12_000
    if isinstance(manifest, Mapping):
        try:
            requested = int(manifest.get("requested_budget_chars") or requested)
        except (TypeError, ValueError):
            requested = 12_000
    cap = _int_env("MLOMEGA_V18_HOT_CAPSULE_MAX_CHARS", 12_000, minimum=1_500, maximum=20_000)
    return max(1_500, min(cap, requested))


def hot_output_budget() -> int:
    return _int_env("MLOMEGA_V18_HOT_OUTPUT_TOKENS", 900, minimum=160, maximum=1_400)


def _compact_episode(episode: Mapping[str, Any] | None) -> dict[str, Any]:
    episode = dict(episode or {})
    return {
        "episode_start_at": episode.get("episode_start_at"),
        "episode_end_at": episode.get("episode_end_at"),
        "before_turn_ids": [str(x) for x in list(episode.get("before_turn_ids") or [])[-2:]],
        "turn_ids": [str(x) for x in list(episode.get("turn_ids") or [])[-12:]],
        "summary": _limit(
            episode.get("summary"),
            _int_env("MLOMEGA_V18_HOT_EPISODE_SUMMARY_CHARS", 2_200, minimum=240, maximum=4_000),
        ),
    }


def _compact_rows(value: Any, *, item_limit: int, text_limit: int) -> list[Any]:
    out: list[Any] = []
    for row in list(value or [])[-item_limit:]:
        if isinstance(row, Mapping):
            clean: dict[str, Any] = {}
            for key in (
                "event_id", "source_id", "person_id", "speaker", "label", "status",
                "confidence", "occurred_at", "captured_at", "text", "summary", "scene_summary",
            ):
                if key not in row:
                    continue
                val = row.get(key)
                clean[key] = _limit(val, text_limit) if isinstance(val, str) else val
            out.append(clean or {"present": True})
        else:
            out.append(_limit(row, text_limit))
    return out


def _compact_fused(fused: Mapping[str, Any] | None) -> dict[str, Any]:
    fused = dict(fused or {})
    summary = fused.get("summary") if isinstance(fused.get("summary"), Mapping) else {}
    return {
        "fused_id": fused.get("fused_id"),
        "person_id": fused.get("person_id"),
        "place": _limit(fused.get("place"), 240),
        "confidence": fused.get("confidence"),
        "readiness": fused.get("readiness"),
        "llm_fusion_status": fused.get("llm_fusion_status"),
        "event_ids": [str(x) for x in list((summary.get("event_ids") if isinstance(summary, Mapping) else None) or fused.get("event_ids") or [])[-12:]],
        "speech": _compact_rows((summary.get("speech") if isinstance(summary, Mapping) else None) or fused.get("speech") or [], item_limit=4, text_limit=360),
        "vision": _compact_rows((summary.get("vision") if isinstance(summary, Mapping) else None) or fused.get("vision") or [], item_limit=3, text_limit=320),
        "people": _compact_rows((summary.get("people") if isinstance(summary, Mapping) else None) or fused.get("people") or [], item_limit=6, text_limit=180),
    }


def _compact_router(route: Mapping[str, Any] | None) -> dict[str, Any]:
    route = dict(route or {})
    router = route.get("router") if isinstance(route.get("router"), Mapping) else {}
    safe_router = {
        key: (_limit(value, 300) if isinstance(value, str) else value)
        for key, value in dict(router).items()
        if key in {"route_status", "reason", "why", "llm_required", "confidence", "triggered_horizons", "error"}
    }
    return {
        "route_id": route.get("route_id"),
        "route_status": route.get("route_status"),
        "triggered_horizons": [str(x).upper() for x in list(route.get("triggered_horizons") or []) if str(x).upper() in {"H0", "H1", "H2"}],
        "router": safe_router,
    }


def _ref_identity(ref: Mapping[str, Any], reason: str | None = None) -> dict[str, Any]:
    raw = dict(ref)
    out = {
        "source_table": raw.get("source_table"),
        "source_id": raw.get("source_id"),
        "occurred_at": raw.get("occurred_at"),
        "content_sha256": raw.get("content_sha256"),
    }
    if reason:
        out["reason"] = reason
    return out


def _ref_stub(ref: Mapping[str, Any], *, text_limit: int) -> tuple[dict[str, Any], bool]:
    raw = dict(ref)
    text = "" if raw.get("text") is None else str(raw.get("text"))
    clipped = _limit(text, text_limit)
    shortened = len(clipped) < len(text)
    return {
        "source_table": raw.get("source_table"),
        "source_id": raw.get("source_id"),
        "occurred_at": raw.get("occurred_at"),
        "evidence_kind": raw.get("evidence_kind"),
        "confidence": raw.get("confidence"),
        "importance": raw.get("importance"),
        "version": raw.get("version"),
        "retrievable": bool(raw.get("retrievable", False)),
        "truncated": bool(raw.get("truncated", False) or shortened),
        "content_sha256": raw.get("content_sha256"),
        "text": clipped,
    }, shortened


def build_hot_capsule_payload(
    *,
    episode: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None,
    fused: Mapping[str, Any] | None,
    route: Mapping[str, Any] | None,
    target_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the exact valid JSON sent to the one-call hot model.

    ``meta`` makes the boundary auditable.  Every dropped source is represented
    as an omission identity; no raw metadata or partial JSON is smuggled past
    the input limit.
    """
    source_manifest = dict(manifest or {})
    scope = dict(source_manifest.get("scope") or {})
    budget = hot_input_budget(source_manifest)
    output_budget = hot_output_budget()
    source_items = [dict(item) for item in list(source_manifest.get("items") or []) if isinstance(item, Mapping)]

    inherited = [
        _ref_identity(item, str(item.get("reason") or "upstream_omission"))
        for item in list(source_manifest.get("omitted_refs") or [])
        if isinstance(item, Mapping)
    ]
    inherited.extend(
        _ref_identity(item, "future_excluded")
        for item in list(source_manifest.get("excluded_future_refs") or [])
        if isinstance(item, Mapping)
    )
    excluded = [
        _ref_identity(item, "future_excluded")
        for item in list(source_manifest.get("excluded_future_refs") or [])
        if isinstance(item, Mapping)
    ]
    deduped = [
        _ref_identity(item, "deduplicated")
        for item in list(source_manifest.get("deduplicated_refs") or [])
        if isinstance(item, Mapping)
    ]
    # These are rendered summaries, not the authoritative omission counts.
    # The limits can shrink during final fitting; unlike mutating the lists
    # directly, that guarantees every reduction step makes measurable progress.
    omission_render_limit = 24
    excluded_render_limit = 12
    deduped_render_limit = 12

    base_manifest: dict[str, Any] = {
        "schema_version": source_manifest.get("schema_version"),
        "purpose": _limit(source_manifest.get("purpose") or "brainlive_live_episode_prediction", 120),
        "scope": {
            "person_id": scope.get("person_id"),
            "live_session_id": scope.get("live_session_id"),
            "as_of": scope.get("as_of"),
            "mode": scope.get("mode"),
        },
        "items": [],
        "omitted_refs": inherited[:omission_render_limit],
        "omitted_ref_count": len(inherited),
        "excluded_future_refs": excluded[:excluded_render_limit],
        "excluded_future_ref_count": len(excluded),
        "deduplicated_refs": deduped[:deduped_render_limit],
        "deduplicated_ref_count": len(deduped),
        # ``incomplete`` means an upstream source is genuinely unavailable.
        # References omitted only to meet the hot prompt budget remain
        # retrievable and must not force an unnecessary H0 refusal.
        "incomplete": bool(source_manifest.get("incomplete")),
        "has_retrievable_omissions": False,
        "requested_budget_chars": budget,
        "rendered_chars": 0,
    }
    payload: dict[str, Any] = {
        "schema_version": VERSION,
        "episode": _compact_episode(episode),
        "manifest": base_manifest,
        "fused_situation": _compact_fused(fused),
        "router": _compact_router(route),
        "hot_scene_context": {
            "self_schema_hot": _compact_rows(source_manifest.get("self_schema_hot") or [], item_limit=5, text_limit=260),
            "scene_focus": _limit(source_manifest.get("scene_focus"), 260),
        },
        "target_ms": max(1, int(target_ms)),
        "input_budget_chars": budget,
        "output_budget_tokens": output_budget,
        "rendered_input_chars": 0,
    }

    new_omissions: list[dict[str, Any]] = []
    for ref in source_items:
        stub, shortened = _ref_stub(ref, text_limit=600)
        candidate_manifest = dict(base_manifest)
        candidate_manifest["items"] = list(base_manifest["items"]) + [stub]
        candidate = dict(payload)
        candidate["manifest"] = candidate_manifest
        if len(json_dumps(candidate)) <= budget:
            base_manifest["items"] = candidate_manifest["items"]
            if shortened:
                new_omissions.append(_ref_identity(ref, "hot_item_text_budget"))
            continue
        identity_only, _ = _ref_stub(ref, text_limit=0)
        candidate_manifest["items"] = list(base_manifest["items"]) + [identity_only]
        candidate["manifest"] = candidate_manifest
        if len(json_dumps(candidate)) <= budget:
            base_manifest["items"] = candidate_manifest["items"]
            new_omissions.append(_ref_identity(ref, "hot_item_text_omitted"))
            continue
        new_omissions.append(_ref_identity(ref, "hot_prompt_budget_exhausted"))

    def _refresh_manifest() -> None:
        omissions = inherited + new_omissions
        base_manifest["omitted_ref_count"] = len(omissions)
        base_manifest["omitted_refs"] = omissions[:max(0, omission_render_limit)]
        base_manifest["excluded_future_refs"] = excluded[:max(0, excluded_render_limit)]
        base_manifest["deduplicated_refs"] = deduped[:max(0, deduped_render_limit)]
        base_manifest["rendered_chars"] = sum(len(str(item.get("text") or "")) for item in base_manifest["items"])
        base_manifest["has_retrievable_omissions"] = bool(new_omissions)
        base_manifest["incomplete"] = bool(source_manifest.get("incomplete"))
        payload["manifest"] = base_manifest

    _refresh_manifest()

    def _measure() -> int:
        return len(json_dumps(payload))

    def _reduce_once() -> bool:
        # Remove ancillary identity-only metadata before discarding evidence
        # text, then shrink local prose, then remove the oldest low-priority
        # readings. Every action is reflected in omitted refs/incomplete.
        nonlocal omission_render_limit, excluded_render_limit, deduped_render_limit
        if deduped_render_limit > 0:
            deduped_render_limit -= 1
            _refresh_manifest()
            return True
        if excluded_render_limit > 0:
            excluded_render_limit -= 1
            _refresh_manifest()
            return True
        if omission_render_limit > 0:
            omission_render_limit -= 1
            _refresh_manifest()
            return True
        summary = str(payload["episode"].get("summary") or "")
        if summary:
            payload["episode"]["summary"] = _limit(summary, max(0, len(summary) - 160))
            return True
        hs = payload.get("hot_scene_context") or {}
        if hs.get("self_schema_hot"):
            hs["self_schema_hot"].pop(0)
            return True
        if hs.get("scene_focus"):
            hs["scene_focus"] = ""
            return True
        for collection in ("speech", "vision", "people"):
            values = payload["fused_situation"].get(collection) or []
            if values:
                values.pop(0)
                return True
        items = base_manifest["items"]
        if items:
            item = items[-1]
            if item.get("text"):
                item["text"] = ""
                item["truncated"] = True
                new_omissions.append(_ref_identity(item, "hot_final_budget_trim"))
            else:
                new_omissions.append(_ref_identity(item, "hot_final_budget_drop"))
                items.pop()
            _refresh_manifest()
            return True
        router = payload["router"].get("router") or {}
        if router:
            payload["router"]["router"] = {}
            return True
        return False

    # First reduce content until the *actual* final JSON can fit.  The metric
    # itself is included in that JSON, so convergence is checked afterwards.
    while _measure() > budget and _reduce_once():
        _refresh_manifest()
    if _measure() > budget:
        raise ValueError(f"hot capsule static payload exceeds input budget: {_measure()}>{budget}")

    for _ in range(8):
        rendered = _measure()
        payload["rendered_input_chars"] = rendered
        actual = _measure()
        if actual <= budget and payload["rendered_input_chars"] == actual:
            break
        while actual > budget and _reduce_once():
            _refresh_manifest()
            payload["rendered_input_chars"] = _measure()
            actual = _measure()
        if actual > budget:
            raise ValueError(f"hot capsule rendered payload exceeds input budget: {actual}>{budget}")
    else:
        raise ValueError("hot capsule rendered size did not converge")

    final_size = len(json_dumps(payload))
    if final_size > budget or int(payload.get("rendered_input_chars") or -1) != final_size:
        raise ValueError(f"hot capsule accounting mismatch: {final_size}>{budget} or metric differs")
    meta = {
        "input_budget_chars": budget,
        "rendered_input_chars": final_size,
        "output_budget_tokens": output_budget,
        "incomplete": bool(base_manifest.get("incomplete")),
        "new_omissions": new_omissions,
        "omitted_ref_count": int(base_manifest.get("omitted_ref_count") or 0),
    }
    return payload, meta
