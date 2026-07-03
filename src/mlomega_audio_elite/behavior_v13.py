from __future__ import annotations

"""Public V13 API.

V13.2 is strict: no V12 baseline, no evidence-only cognitive path, no keyword or
regex analyst. The public commands call the strict Qwen/Ollama Brain 2.0 layer.
"""

from typing import Any

from .brain2_strict_v13_2 import (
    STRICT_VERSION as V13_VERSION,
    COMPLETE_TARGETS as V13_TARGETS,
    audit_strict_v13_plan,
    build_strict_v13_all,
    build_strict_v13_for_conversation,
    predict_strict_v13,
    strict_v13_overview,
    verify_strict_v13_prediction,
)


class V13LLMRequired(RuntimeError):
    pass


def _reject_no_llm(require_llm: bool | None) -> None:
    if require_llm is False:
        raise V13LLMRequired("V13.2 strict refuse require_llm=False: pas de mode evidence-only, pas de faux cerveau.")


def audit_v13_plan(*, persist: bool = True) -> dict[str, Any]:
    from .voice_learning import ensure_voice_learning_schema
    from .audio_preprocess import ensure_audio_preprocess_schema
    from .brain2_flow_v13_3 import ensure_brain2_flow_schema
    ensure_voice_learning_schema(); ensure_audio_preprocess_schema(); ensure_brain2_flow_schema()
    return audit_strict_v13_plan(persist=persist)


def build_v13_for_conversation(conversation_id: str, *, require_llm: bool | None = None, max_episodes: int | None = None, person_id: str | None = None, run_extensions: bool = True) -> dict[str, Any]:
    _reject_no_llm(require_llm)
    from .brain2_flow_v13_3 import build_subtopic_segments, discover_latent_outcomes_from_conversation, ensure_brain2_flow_schema
    from .autonomous_v13_4 import run_autonomous_insights, ensure_autonomous_schema
    ensure_brain2_flow_schema(); ensure_autonomous_schema()
    core = build_strict_v13_for_conversation(conversation_id, max_episodes=max_episodes, person_id=person_id)
    if not run_extensions:
        return core
    subtopics = build_subtopic_segments(conversation_id)
    latent = discover_latent_outcomes_from_conversation(conversation_id)
    autonomous = run_autonomous_insights(conversation_id, trigger_type="post_v13_build")
    return {**core, "v13_3_subtopics": subtopics, "v13_3_latent_outcomes": latent, "v13_4_autonomous": autonomous}


def build_v13_all(*, require_llm: bool | None = None, max_episodes_per_conversation: int | None = None) -> dict[str, Any]:
    _reject_no_llm(require_llm)
    from .db import connect
    from .brain2_flow_v13_3 import ensure_brain2_flow_schema
    from .autonomous_v13_4 import ensure_autonomous_schema
    ensure_brain2_flow_schema(); ensure_autonomous_schema()
    with connect() as con:
        convs = [r["conversation_id"] for r in con.execute("SELECT conversation_id FROM conversations ORDER BY started_at, created_at")]
    return {"version": V13_VERSION, "mode": "strict_qwen_no_heuristics_v13_3_flow", "conversations": len(convs), "results": [build_v13_for_conversation(cid, max_episodes=max_episodes_per_conversation) for cid in convs]}


def predict_v13(target: str, context: str, *, person_id: str | None = None, horizon: str = "next", require_llm: bool | None = None) -> dict[str, Any]:
    _reject_no_llm(require_llm)
    return predict_strict_v13(target, context, person_id=person_id, horizon=horizon)


def verify_v13_prediction(prediction_id: str, observed_value: str, *, match_score: float | None = None, note: str | None = None, require_llm: bool | None = None) -> dict[str, Any]:
    _reject_no_llm(require_llm)
    return verify_strict_v13_prediction(prediction_id, observed_value, match_score=match_score, note=note)


def v13_overview() -> dict[str, Any]:
    from .voice_learning import ensure_voice_learning_schema
    from .audio_preprocess import ensure_audio_preprocess_schema
    from .brain2_flow_v13_3 import ensure_brain2_flow_schema
    ensure_voice_learning_schema(); ensure_audio_preprocess_schema(); ensure_brain2_flow_schema()
    from .autonomous_v13_4 import ensure_autonomous_schema
    ensure_autonomous_schema()
    return strict_v13_overview()

# V18: public V13 entry points require explicit ownership and use the candidate-only autonomous layer.
from .v18_autonomous import install_behavior as _install_v18_behavior
_globals_v18_behavior = _install_v18_behavior(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_behavior)
