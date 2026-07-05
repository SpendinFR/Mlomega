from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

CONTRACTS_VERSION = "v19.0"
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contracts_version: str = CONTRACTS_VERSION

class Pose(StrictModel):
    position: list[float] = Field(default_factory=list)
    rotation: list[float] = Field(default_factory=list)

class FrameEnvelope(StrictModel):
    session_id: str; frame_id: str; capture_monotonic_ns: int; captured_at_utc: str
    pose: Pose; intrinsics: dict[str, Any] | None = None
    rotation: Literal[0,90,180,270] = 0; source: str
    # E37 §5: a placeholder envelope (gateway._placeholder_envelope, pose 0,0,0) is a
    # synthetic frame with no real head pose. It must NEVER feed the spatial map /
    # SceneDelta pose calculations. Real WebRTC frames leave this True; the
    # placeholder path sets it False and consumers skip its pose.
    pose_valid: bool = True

class LocalTrack(StrictModel):
    session_id: str; track_id: str; source_frame_id: str; kind: str
    bbox_or_mask: dict[str, Any]; velocity_screen: list[float] = Field(default_factory=list)
    visibility: float; confidence: float; observed_at_monotonic_ns: int

class SceneDelta(StrictModel):
    session_id: str; source_frame_id: str; entities: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list); changes: list[dict[str, Any]] = Field(default_factory=list)
    map_quality: float = 0.0; evidence_refs: list[str] = Field(default_factory=list); expires_at: str | None = None

class ReflexEvent(StrictModel):
    session_id: str; source_frame_id: str; skill: str; prediction: dict[str, Any]
    horizon_ms: int; confidence: float; severity: str; evidence_refs: list[str] = Field(default_factory=list); aggregate_key: str

TruthLevel = Literal['observed','probable','remembered','inferred','replay']
class UIIntent(StrictModel):
    ui_intent_id: str; producer: Literal['ultralive','visionrt','brainlive']; source_frame_id: str | None = None
    target_track_id: str | None = None; entity_id: str | None = None; component: str; anchor: dict[str, Any]
    content: dict[str, Any]; truth_level: TruthLevel; confidence: float; priority: float; ttl_ms: int
    ui_hint: dict[str, Any] = Field(default_factory=dict); evidence_refs: list[str] = Field(default_factory=list); delivery_id: str | None = None

class UIReceipt(StrictModel):
    ui_intent_id: str; delivery_id: str | None = None
    event: Literal['displayed','seen','acted','dismissed','corrected']; observed_at: str
    local_track_state: dict[str, Any] = Field(default_factory=dict); user_action: dict[str, Any] | None = None; source: str

class HotSceneContext(StrictModel):
    session_id: str; as_of: str; place: dict[str, Any] | None = None; map_quality: float = 0.0
    focus: dict[str, Any] | None = None; visible_entities: list[dict[str, Any]] = Field(default_factory=list)
    people_identified: list[dict[str, Any]] = Field(default_factory=list); activity: dict[str, Any] | None = None
    translation_active: dict[str, Any] | None = None; changes: list[dict[str, Any]] = Field(default_factory=list)
    reflex_events: list[ReflexEvent] = Field(default_factory=list); brain2_memory: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list); omissions: list[str] = Field(default_factory=list)

class EvidenceEvent(StrictModel):
    event_type: str; occurred_at: str; session_id: str; entity: dict[str, Any] | None = None
    observation: dict[str, Any]; place: dict[str, Any] | None = None
    truth_level: Literal['observed','inferred','consolidated','probable','remembered']
    confidence: float; evidence: list[dict[str, Any]] = Field(default_factory=list); provenance: dict[str, Any]
