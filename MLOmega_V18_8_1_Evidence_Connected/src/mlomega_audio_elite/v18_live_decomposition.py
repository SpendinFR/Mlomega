"""V18 decomposition of the live LLM boundary.

The historical live call asked one model response to simultaneously observe,
predict, propose interventions and write Brain2 notes.  That made one malformed
or overbroad result fan out into several artefact families.  V18.1 executes
three narrowly contracted stages and persists their provenance before a merged
payload is allowed to reach the canonical writer.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from .integrity_v176 import (
    AffordanceContract,
    ContractValidationError,
    EventContract,
    ForecastContract,
    InterventionContract,
    LifeHypothesisContract,
    NeedPredictionContract,
    WorldStateContract,
    validate_brainlive_output,
)
from .utils import json_dumps, now_iso
from .v18_runtime_hardening import validate_resolvable_semantic_output


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_reasoning_stages_v18(
  stage_run_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  stage_name TEXT NOT NULL CHECK(stage_name IN ('observation','forecast','intervention')),
  status TEXT NOT NULL CHECK(status IN ('started','ok','quarantined','error')),
  input_manifest_id TEXT,
  input_hash TEXT NOT NULL,
  output_json TEXT DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, stage_name)
);
CREATE INDEX IF NOT EXISTS idx_bl_reasoning_stages_run
  ON brainlive_reasoning_stages_v18(run_id, stage_name, status);
"""

OBSERVATION_SCHEMA = {
    "world_state": {"active_mode": "conversation|work|routine|transition|social|rest|unknown|other", "probable_activity": [], "confidence": 0.0, "evidence": [{"source_table": "", "source_id": ""}], "counter_evidence": [{"source_table": "", "source_id": ""}]},
    "events": [],
    "need_predictions": [],
    "affordances": [],
}
FORECAST_SCHEMA = {
    "forecasts": [],
    "life_hypotheses": [],
    "watch_next": [],
}
INTERVENTION_SCHEMA = {
    "interventions": [],
    "notes_for_brain2": [],
}

# A controlled bridge for an old model adapter that returns the historical
# complete BrainLive document despite a stage-specific V18 schema hint.  It is
# intentionally exact-shape only: partial stage responses with extra keys are
# still rejected by the strict validators below.
_FULL_BRAINLIVE_KEYS = {
    "world_state",
    "events",
    "need_predictions",
    "affordances",
    "forecasts",
    "life_hypotheses",
    "interventions",
    "notes_for_brain2",
    "watch_next",
}
_STAGE_KEYS = {
    "observation": ("world_state", "events", "need_predictions", "affordances"),
    "forecast": ("forecasts", "life_hypotheses", "watch_next"),
    "intervention": ("interventions", "notes_for_brain2"),
}


def _project_legacy_full_output(stage_name: str, payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload) == _FULL_BRAINLIVE_KEYS:
        return {key: payload[key] for key in _STAGE_KEYS[stage_name]}
    return payload


def _hash(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def _strict_keys(payload: Any, keys: set[str], stage: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractValidationError(f"{stage} stage must return a JSON object")
    found = set(payload)
    if found != keys:
        missing = sorted(keys - found)
        extra = sorted(found - keys)
        raise ContractValidationError(f"{stage} stage keys mismatch: missing={missing}, extra={extra}")
    return payload


def _validate_observation(payload: Any) -> dict[str, Any]:
    p = _strict_keys(payload, {"world_state", "events", "need_predictions", "affordances"}, "observation")
    try:
        return {
            "world_state": WorldStateContract.model_validate(p["world_state"]).model_dump(mode="python"),
            "events": [EventContract.model_validate(v).model_dump(mode="python") for v in p["events"]],
            "need_predictions": [NeedPredictionContract.model_validate(v).model_dump(mode="python") for v in p["need_predictions"]],
            "affordances": [AffordanceContract.model_validate(v).model_dump(mode="python") for v in p["affordances"]],
        }
    except Exception as exc:
        raise ContractValidationError(f"invalid observation stage contract: {exc}") from exc


def _validate_forecast(payload: Any) -> dict[str, Any]:
    p = _strict_keys(payload, {"forecasts", "life_hypotheses", "watch_next"}, "forecast")
    try:
        if not isinstance(p["watch_next"], list):
            raise ValueError("watch_next must be a list")
        return {
            "forecasts": [ForecastContract.model_validate(v).model_dump(mode="python") for v in p["forecasts"]],
            "life_hypotheses": [LifeHypothesisContract.model_validate(v).model_dump(mode="python") for v in p["life_hypotheses"]],
            "watch_next": p["watch_next"],
        }
    except Exception as exc:
        raise ContractValidationError(f"invalid forecast stage contract: {exc}") from exc


def _validate_intervention(payload: Any) -> dict[str, Any]:
    p = _strict_keys(payload, {"interventions", "notes_for_brain2"}, "intervention")
    try:
        if not isinstance(p["notes_for_brain2"], list):
            raise ValueError("notes_for_brain2 must be a list")
        return {
            "interventions": [InterventionContract.model_validate(v).model_dump(mode="python") for v in p["interventions"]],
            "notes_for_brain2": p["notes_for_brain2"],
        }
    except Exception as exc:
        raise ContractValidationError(f"invalid intervention stage contract: {exc}") from exc


def _ensure_schema(module: Any) -> None:
    module.ensure_brainlive_schema()
    with module.connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def _write_stage(module: Any, **kwargs: Any) -> None:
    """Persist a stage result without exposing a partial result as live output."""
    run_id = kwargs["run_id"]
    stage_name = kwargs["stage_name"]
    input_payload = kwargs["input_payload"]
    created_at = kwargs["created_at"]
    with module.connect() as con:
        module.upsert(
            con,
            "brainlive_reasoning_stages_v18",
            {
                "stage_run_id": module.stable_id("blstage", run_id, stage_name),
                "run_id": run_id,
                "live_session_id": kwargs["live_session_id"],
                "person_id": kwargs["person_id"],
                "stage_name": stage_name,
                "status": kwargs["status"],
                "input_manifest_id": str((input_payload.get("context_manifest") or {}).get("manifest_id") or "") or None,
                "input_hash": _hash(input_payload),
                "output_json": json_dumps(kwargs.get("output") or {}),
                "error_text": kwargs.get("error_text"),
                "created_at": created_at,
                "updated_at": now_iso(),
            },
            "stage_run_id",
        )
        con.commit()


def _stage_call(
    module: Any,
    *,
    client: Any,
    run_id: str,
    live_session_id: str,
    person_id: str,
    stage_name: str,
    system: str,
    prompt_payload: dict[str, Any],
    schema: dict[str, Any],
    validator: Any,
    timeout: float,
    created_at: str,
) -> dict[str, Any]:
    raw: Any = None
    try:
        raw = client.require_json(system, json_dumps(prompt_payload), schema_hint=schema, timeout=timeout)
        value = validator(_project_legacy_full_output(stage_name, raw))
    except ContractValidationError as exc:
        captured = raw if raw is not None else {"raw_output": str(getattr(exc, "raw", "") or "")}
        _write_stage(
            module, run_id=run_id, live_session_id=live_session_id, person_id=person_id,
            stage_name=stage_name, input_payload=prompt_payload, output=captured,
            status="quarantined", error_text=str(exc)[:2000], created_at=created_at,
        )
        setattr(exc, "v18_raw_payload", captured)
        raise
    except Exception as exc:
        captured = raw if raw is not None else {"raw_output": str(getattr(exc, "raw", "") or "")}
        _write_stage(
            module, run_id=run_id, live_session_id=live_session_id, person_id=person_id,
            stage_name=stage_name, input_payload=prompt_payload, output=captured,
            status="error", error_text=str(exc)[:2000], created_at=created_at,
        )
        try:
            setattr(exc, "v18_raw_payload", captured)
        except Exception:
            pass
        raise
    _write_stage(
        module, run_id=run_id, live_session_id=live_session_id, person_id=person_id,
        stage_name=stage_name, input_payload=prompt_payload, output=value,
        status="ok", error_text=None, created_at=created_at,
    )
    return value

def run_decomposed(module: Any, live_session_id: str, *, mode: str, timeout: float, active_people: list[str] | None, limit: int) -> dict[str, Any]:
    _ensure_schema(module)
    ctx_result = module.build_active_context(live_session_id, active_people=active_people, limit=limit)
    active_context_id = ctx_result["active_context_id"]
    context = ctx_result["context"]
    now = now_iso()
    run_id = module.stable_id("blrun", live_session_id, mode, now)
    person_id = context["session"]["person_id"]
    started = time.time()
    status = "ok"
    error_text: str | None = None
    invalid_payload: dict[str, Any] | None = None
    q = module._empty_llm_required_output("decomposed live inference not executed")
    allow_incomplete = os.environ.get("MLOMEGA_V18_ALLOW_INCOMPLETE_CONTEXT_INFERENCE", "false").strip().lower() in {"1", "true", "yes", "on"}

    if bool(context.get("context_incomplete")) and not allow_incomplete:
        status = "context_incomplete"
        error_text = "V18 refused decomposed inference from an incomplete context manifest."
        q = module._empty_llm_required_output(error_text)
    else:
        try:
            client = module.OllamaJsonClient()
            common = {
                "mode": mode,
                "session": context.get("session"),
                "active_people": context.get("active_people") or [],
                "context_manifest": context.get("context_manifest"),
                "retrieval_policy": context.get("retrieval_policy"),
            }
            obs_input = {
                **common,
                "task": "observation",
                "instruction": "Produce only a bounded observation/fusion: world_state, observed events, predicted near needs and affordances. Do not forecast, intervene or write Brain2 notes.",
            }
            obs = _stage_call(
                module, client=client, run_id=run_id, live_session_id=live_session_id, person_id=person_id,
                stage_name="observation", system="You are BrainLive V18 observation/fusion. Return strict JSON only; do not infer facts absent from the manifest.",
                prompt_payload=obs_input, schema=OBSERVATION_SCHEMA, validator=_validate_observation,
                timeout=timeout, created_at=now,
            )
            forecast_input = {
                **common,
                "task": "forecast",
                "observation": obs,
                "instruction": "Generate only H0/H1/H2 forecasts, life hypotheses and watch_next from the validated observation and manifest references. Do not propose an intervention.",
            }
            forecast = _stage_call(
                module, client=client, run_id=run_id, live_session_id=live_session_id, person_id=person_id,
                stage_name="forecast", system="You are BrainLive V18 forecast. Return strict JSON only. Use calibrated uncertainty and no unavailable evidence.",
                prompt_payload=forecast_input, schema=FORECAST_SCHEMA, validator=_validate_forecast,
                timeout=timeout, created_at=now,
            )
            intervention_input = {
                "mode": mode,
                "task": "intervention",
                "session": common["session"],
                "validated_observation": obs,
                "validated_forecast": forecast,
                "instruction": "Propose only value-gated interventions and Brain2 notes. Do not repeat observations or emit forecasts.",
            }
            intervention = _stage_call(
                module, client=client, run_id=run_id, live_session_id=live_session_id, person_id=person_id,
                stage_name="intervention", system="You are BrainLive V18 intervention. Preserve autonomy; return strict JSON only.",
                prompt_payload=intervention_input, schema=INTERVENTION_SCHEMA, validator=_validate_intervention,
                timeout=timeout, created_at=now,
            )
            q = validate_brainlive_output({**obs, **forecast, **intervention})
            manifest = context.get("context_manifest") or {}
            as_of = str((manifest.get("scope") or {}).get("as_of") or now)
            q = validate_resolvable_semantic_output(
                q,
                context_manifest=manifest,
                person_id=person_id,
                live_session_id=live_session_id,
                as_of=as_of,
            )
        except ContractValidationError as exc:
            status = "quarantined_invalid_llm_output"
            error_text = str(exc)[:2000]
            invalid_payload = getattr(exc, "v18_raw_payload", None)
            q = module._empty_llm_required_output(error_text)
        except Exception as exc:
            status = "error"
            error_text = str(exc)[:2000]
            invalid_payload = getattr(exc, "v18_raw_payload", None)
            q = module._empty_llm_required_output(error_text or "decomposed_live_error")

    latency_ms = int((time.time() - started) * 1000)
    with module.connect() as con:
        module.upsert(
            con,
            "brainlive_analysis_runs",
            {
                "run_id": run_id,
                "live_session_id": live_session_id,
                "event_id": None,
                "active_context_id": active_context_id,
                "person_id": person_id,
                "analysis_mode": mode,
                "model": "ollama_decomposed_v18" if status == "ok" else "none_or_failed_decomposed_v18",
                "prompt_context_json": json_dumps({"active_context_id": active_context_id, "execution_mode": "decomposed_v18", "stages": ["observation", "forecast", "intervention"]}),
                "qwen_json": json_dumps(q),
                "latency_ms": latency_ms,
                "status": status,
                "error_text": error_text,
                "created_at": now,
            },
            "run_id",
        )
        counts = module._persist_brainlive_output(con, live_session_id=live_session_id, run_id=run_id, person_id=person_id, q=q, now=now) if status == "ok" else {"world_states": 0, "events": 0, "needs": 0, "affordances": 0, "forecasts": 0, "hypotheses": 0, "interventions": 0}
        con.commit()
    if status == "quarantined_invalid_llm_output":
        module.quarantine(category="invalid_llm_contract", reason=error_text or "invalid decomposed BrainLive LLM payload", raw_payload=invalid_payload, run_id=run_id, source_table="brainlive_analysis_runs", source_id=run_id, person_id=person_id)
    return {
        "run_id": run_id,
        "live_session_id": live_session_id,
        "active_context_id": active_context_id,
        "status": status,
        "error_text": error_text,
        "latency_ms": latency_ms,
        "counts": counts,
        "output": q,
        "execution_mode": "decomposed_v18",
        "stages": ["observation", "forecast", "intervention"],
    }


def install(module: Any) -> dict[str, Any]:
    """Install the decomposed default while retaining an explicit diagnostic escape."""
    previous = module.run_brainlive

    def run_brainlive(live_session_id: str, *, mode: str = "deep_live", use_llm: bool = True, timeout: float = 480.0, active_people: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        enabled = os.environ.get("MLOMEGA_V18_DECOMPOSED_LIVE", "true").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled or not use_llm:
            return previous(live_session_id, mode=mode, use_llm=use_llm, timeout=timeout, active_people=active_people, limit=limit)
        return run_decomposed(module, live_session_id, mode=mode, timeout=timeout, active_people=active_people, limit=limit)

    return {"run_brainlive": run_brainlive}
