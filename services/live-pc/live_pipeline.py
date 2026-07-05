from __future__ import annotations

"""LivePipeline — wires the gateway to VisionRT and AudioRT (E27 orchestration).

Video: the gateway (``AiortcIngress`` / ``LatestFrameQueue``, queue=1) feeds
:class:`VisionRT.process_frame` on every decoded frame. SceneDeltas and focus
UIIntents are pushed to the device over the DataChannel (``ingress.send_ui_intent``)
and mirrored to a callback for the future WorldBrain (E28).

Audio: raw audio chunks (from an aiortc AudioStreamTrack, or the test feeder)
flow into :class:`AudioRT.push_audio`; subtitle UIIntents go directly over the
same DataChannel (reflex path §3.2 — never through the BrainLive queue).

Degraded control: a :class:`DegradedStateMachine` turns GpuArbiter/heartbeat
signals into an action level, applied to VisionRT (detector floor / pause
changes / refuse VLM) and surfaced to the StatusBar. The tracker and subtitles
are never touched (handoff §3.6).

Metrics: :meth:`metrics` merges VisionRT + AudioRT + queue counters, exposed by
``create_metrics_app`` at ``/metrics``.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load sibling service modules robustly (package or importlib test loading).
_tracking = _load_sibling("v19_tracking", "tracking.py")
visionrt = _load_sibling("v19_visionrt", "visionrt.py")
audiort = _load_sibling("v19_audiort", "audiort.py")
degraded = _load_sibling("v19_degraded", "degraded.py")
spatial = _load_sibling("v19_spatial", "spatial.py")
worldbrain = _load_sibling("v19_worldbrain", "worldbrain.py")
scene_adapter = _load_sibling("v19_scene_adapter", "brainlive_scene_adapter.py")
conversation_bridge = _load_sibling("v19_conversation_bridge", "conversation_bridge.py")
face_identity = _load_sibling("face_identity", "face_identity.py")
voice_identity_live = _load_sibling("voice_identity_live", "voice_identity_live.py")
identity_fusion = _load_sibling("identity_fusion", "identity_fusion.py")
enrollment_watcher = _load_sibling("enrollment_watcher", "enrollment_watcher.py")
llm_providers = _load_sibling("v19_llm_providers", "llm_providers.py")
memory_query = _load_sibling("v19_memory_query", "memory_query.py")
intent_router = _load_sibling("v19_intent_router", "intent_router.py")
proactive_context = _load_sibling("v19_proactive_context", "proactive_context.py")
predictive_retrieval_live = _load_sibling("v19_predictive_retrieval_live", "predictive_retrieval_live.py")
live_discourse = _load_sibling("v19_live_discourse", "live_discourse.py")
morning_briefing = _load_sibling("v19_morning_briefing", "morning_briefing.py")
tts_local = _load_sibling("v19_tts_local", "tts_local.py")
replay_service_mod = _load_sibling("v19_replay_service", "replay_service.py")
endpoint_resolver = _load_sibling("v19_endpoint_resolver", "endpoint_resolver.py")
stranger_profile = _load_sibling("v19_stranger_profile", "stranger_profile.py")
audio_archive = _load_sibling("v19_audio_archive", "audio_archive.py")
owner_setup = _load_sibling("v19_owner_setup", "owner_setup.py")
hypothesis_engine = _load_sibling("v19_hypothesis_engine", "hypothesis_engine.py")
attribute_memory = _load_sibling("v19_attribute_memory", "attribute_memory.py")
routine_associations = _load_sibling("v19_routine_associations", "routine_associations.py")


def load_profile(profile_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(profile_path) if profile_path else _ROOT / "configs" / "profiles" / "rtx3070.yaml"
    if not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_user_profile(path: Path | str | None = None) -> dict[str, Any]:
    """Read ``configs/user_profile.yaml`` (handoff §3.5). Returns {} if absent.

    This is the capability profile written by ``setup_profile.ps1``: ``display``
    (``companion_web`` | ``phone_only`` | ``xreal_one_pro`` | ``spectacles``),
    ``capture``, ``llm``, ``vision``, ``asr``, ``cloud_data_policy``. It is read
    by ``RUN_MLOMEGA_V19.ps1 -SimOnly`` and by :class:`LivePipeline` so that the
    ``phone_only`` display path is honoured end to end (E29).
    """
    p = Path(path) if path else _ROOT / "configs" / "user_profile.yaml"
    if not p.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# Frame rotation applied on the PC when ``FrameEnvelope.rotation`` says the device
# sensor was rotated. The device streams the pixels as captured (rotated); the PC
# un-rotates them so the detector/OCR see upright content. This is the PC half of
# the OrientationGuard capture-only path (E29 §3a). Values are the inverse of the
# device rotation so that ``apply(device_rotation)`` yields an upright frame.
_ROTATE_UNDO = {
    0: None,
    90: "ROTATE_90_COUNTERCLOCKWISE",
    180: "ROTATE_180",
    270: "ROTATE_90_CLOCKWISE",
}


def deorient_frame(frame_bgr: np.ndarray, rotation: int) -> np.ndarray:
    """Return an upright frame given the device ``rotation`` (0/90/180/270).

    ``rotation`` is the sensor rotation the device stamped in the envelope; the
    PC un-rotates so vision always sees an upright image (handoff capture-only).
    When cv2 is unavailable it falls back to numpy ``rot90``.
    """
    r = int(rotation or 0) % 360
    if r == 0:
        return frame_bgr
    try:
        import cv2

        code = getattr(cv2, _ROTATE_UNDO.get(r, ""), None) if _ROTATE_UNDO.get(r) else None
        if code is not None:
            return cv2.rotate(frame_bgr, code)
    except Exception:
        pass
    # numpy fallback: rot90 k times (counter-clockwise); undo device CW rotation.
    k = {90: 1, 180: 2, 270: 3}.get(r, 0)
    return np.ascontiguousarray(np.rot90(frame_bgr, k=k))


class LivePipeline:
    def __init__(
        self,
        *,
        session_id: str = "live",
        ingress: Any = None,
        arbiter: Any = None,
        profile_path: Path | str | None = None,
        detector_model: str | Path | None = None,
        keyframe_sink: Callable[[np.ndarray, Any], Any] | None = None,
        on_scene_delta: Callable[[dict[str, Any]], Any] | None = None,
        enable_detector: bool = True,
        enable_worldbrain: bool = False,
        person_id: str | None = None,
        db_path: Any = None,
        known_people: dict[str, dict[str, Any]] | None = None,
        user_profile: dict[str, Any] | None = None,
        apply_rotation: bool = True,
        enable_conversation: bool = False,
        conversation_bridge: Any = None,
        enable_identity: bool = False,
        face_service_db_path: Any = None,
        face_embedder: Any = None,
        voice_embedder: Any = None,
        identity_frame_interval: int = 30,
        enable_intents: bool = False,
        vision_focus_handler: Callable[[dict[str, Any]], Any] | None = None,
        enable_proactivity: bool = False,
        predictive_backend: Any = None,
        enable_tts: bool = False,
        enable_replay: bool = False,
        enable_stranger_profiles: bool = False,
        enable_audio_archive: bool = False,
        enable_fine_intel: bool = False,
        fine_intel_llm: Any = None,
        active_link: str = "lan",
    ) -> None:
        self.session_id = session_id
        self.ingress = ingress
        self.arbiter = arbiter
        self.profile = load_profile(profile_path)
        # Capability profile (handoff §3.5): display=companion_web|phone_only|...
        self.user_profile = user_profile if user_profile is not None else load_user_profile()
        self.display = str(self.user_profile.get("display", "companion_web") or "companion_web")
        self.apply_rotation = apply_rotation
        self.rotation_corrections = 0
        vcfg = self.profile.get("vision", {}) if isinstance(self.profile, dict) else {}
        acfg = self.profile.get("audio", {}) if isinstance(self.profile, dict) else {}

        detector = None
        if enable_detector:
            model = detector_model or (vcfg.get("detector", {}) or {}).get("model_path") or (
                _ROOT / "models" / "yolox_nano.onnx"
            )
            try:
                if Path(model).exists():
                    dcfg = vcfg.get("detector", {}) or {}
                    detector = visionrt.YoloxDetector(
                        model,
                        input_size=int(dcfg.get("input_size", 416)),
                        score_threshold=float(dcfg.get("score_threshold", 0.30)),
                        nms_threshold=float(dcfg.get("nms_threshold", 0.45)),
                    )
            except Exception:
                detector = None
        self.detector_available = detector is not None

        cadence = visionrt.AdaptiveCadence(
            fps_min=float(vcfg.get("detector_fps_min", 5)),
            fps_max=float(vcfg.get("detector_fps_max", 15)),
            motion_low=float(vcfg.get("motion_low", 0.015)),
            motion_high=float(vcfg.get("motion_high", 0.06)),
        )
        keyframes = visionrt.KeyframeSelector(
            change_threshold=float(vcfg.get("keyframe_change_threshold", 0.35)),
            min_interval_s=float(vcfg.get("keyframe_min_interval_s", 3.0)),
        )
        self._external_scene_cb = on_scene_delta
        self.vision = visionrt.VisionRT(
            detector=detector,
            cadence=cadence,
            keyframes=keyframes,
            arbiter=arbiter,
            session_id=session_id,
            on_scene_delta=self._on_scene_delta,
            on_ui_intent=self._push_intent,
            keyframe_sink=keyframe_sink,
        )
        self.audio = audiort.AudioRT(
            session_id=session_id,
            target_language=str((acfg.get("asr", {}) or {}).get("target_language", "fr")),
            arbiter=arbiter,
            on_intent=self._push_intent,
            on_segment=self._on_audio_segment,
        )
        # E36 §1: distinct lan/wan network thresholds. Outside-the-home the link is a
        # VPN tunnel over 4G/5G — a higher latency ceiling + a lower target video
        # height so the tunnel is not saturated. The PC detector cadence never
        # changes with the link (it runs locally), and the device reflex paths do
        # not depend on the PC. ``active_link`` (lan|wan) selects the profile.
        dcfg = self.profile.get("degraded", {}) if isinstance(self.profile, dict) else {}
        self.network_profiles = degraded.network_profiles_from_config(dcfg)
        self.active_link = str(active_link or "lan")
        # E36 §1: the resolved PC endpoint that this session reached the PC through
        # (LAN or tunnel); surfaced on /metrics. Set by set_active_endpoint().
        self.active_endpoint: str | None = None
        self.degraded = degraded.DegradedStateMachine(
            thresholds=degraded.thresholds_for_link(self.network_profiles, self.active_link)
        )
        self._status_cb: Callable[[dict[str, Any]], Any] | None = None
        self._last_action = degraded.ACTION_NOMINAL

        # ---- WorldBrain (E28): the spatial/relational present ----------------
        wcfg = self.profile.get("worldbrain", {}) if isinstance(self.profile, dict) else {}
        self.person_id = person_id or "me"
        self.db_path = db_path
        self.spatial: Any = None
        self.worldbrain: Any = None
        self.scene_adapter: Any = None
        # ---- Proactivity (E34): nightly engines → live + dense retrieval --------
        self.enable_proactivity = enable_proactivity
        self.proactive: Any = None
        self.predictive_retrieval: Any = None
        self.morning_briefing: Any = None
        if enable_proactivity:
            self.proactive = proactive_context.ProactiveContext(
                person_id=self.person_id, db_path=db_path,
            )
            self.predictive_retrieval = predictive_retrieval_live.PredictiveRetrievalLive(
                backend=predictive_backend, db_path=db_path,
            )
        if enable_worldbrain:
            self.spatial = spatial.PoseKeyframeMap(
                spatial.SpatialConfig(
                    min_map_quality_for_bearing=float(wcfg.get("min_map_quality_for_bearing", 0.35)),
                )
            )
            self.worldbrain = worldbrain.WorldBrain(
                person_id=self.person_id,
                live_session_id=session_id,
                config=worldbrain.WorldBrainConfig(
                    promote_min_observations=int(wcfg.get("promote_min_observations", 3)),
                    promote_min_confidence=float(wcfg.get("promote_min_confidence", 0.35)),
                ),
                db_path=db_path,
                spatial=self.spatial,
            )
            self.scene_adapter = scene_adapter.BrainLiveSceneAdapter(
                person_id=self.person_id,
                live_session_id=session_id,
                worldbrain=self.worldbrain,
                db_path=db_path,
                known_people=known_people,
                proactive=self.proactive,
                predictive_retrieval=self.predictive_retrieval,
                on_entity_hot_update=self._push_intent,
            )
            # E34 §2: load the day's open items so they are ready at session start.
            if self.proactive is not None:
                try:
                    self.proactive.refresh()
                except Exception:
                    pass
            # E34 §6: the morning briefing is built on the first session of the day.
            if enable_proactivity:
                self.morning_briefing = morning_briefing.MorningBriefing(
                    person_id=self.person_id, live_session_id=session_id,
                    proactive=self.proactive, worldbrain=self.worldbrain, db_path=db_path,
                )

        # ---- ConversationBridge (E31): live transcripts -> BrainLive loop -----
        # Final AudioRT segments are injected into the V18.8 conversational engine
        # (turn buffer -> plan_live_dispatch -> hot loop -> H1 -> delivery queue).
        # The bridge owns its OWN BrainLive live session (a real brainlive_sessions
        # row via start_live_session), distinct from the arbitrary transport
        # ``session_id`` used for scene deltas.
        self.conversation: Any = conversation_bridge
        if self.conversation is None and enable_conversation:
            self.conversation = globals()["conversation_bridge"].ConversationBridge(
                person_id=self.person_id,
            )
        # ---- LiveDiscourse (E34 §4): fine discourse analysis of live turns off
        # the hot path — final turns are batched and analysed by the core
        # microscope/discourse pipeline in a background worker (never blocks).
        self.live_discourse: Any = None
        if enable_proactivity and enable_conversation:
            self.live_discourse = live_discourse.LiveDiscourse(person_id=self.person_id)

        # ---- Identity (E32): face + voice + fusion + enrollment ---------------
        # Face runs on person crops at an ECONOMICAL cadence (new person track or
        # every N frames), voice on final segments, fusion names WorldBrain person
        # entities above threshold (scene adapter's ContextCard trigger fires
        # naturally), enrollment_watcher pre-routes "retiens : c'est X" commands.
        self.identity_frame_interval = max(1, int(identity_frame_interval))
        self._frame_counter = 0
        self._identity_seen_tracks: set[str] = set()
        self.face: Any = None
        self.voice_identity: Any = None
        self.fusion: Any = None
        self.enrollment: Any = None
        if enable_identity:
            fcfg = face_identity.FaceConfig.from_env(self.profile)
            try:
                self.face = face_identity.FaceIdentity(
                    config=fcfg, embedder=face_embedder,
                    service_db_path=face_service_db_path, arbiter=arbiter,
                )
            except Exception:
                self.face = None
            self.voice_identity = voice_identity_live.VoiceIdentityLive(embedder=voice_embedder)
            self.fusion = identity_fusion.IdentityFusion(
                worldbrain=self.worldbrain, scene_adapter=self.scene_adapter,
            )
            self.enrollment = enrollment_watcher.EnrollmentWatcher(
                face_identity=self.face, voice_identity=self.voice_identity,
                fusion=self.fusion, worldbrain=self.worldbrain, person_id=self.person_id,
                emit_ui_intent=self._push_intent,
            )
        elif self.worldbrain is not None:
            # E35 §3: even without face/voice identity, a WorldBrain-backed watcher
            # handles OBJECT / PLACE voice corrections ("ce n'est pas mon téléphone").
            self.enrollment = enrollment_watcher.EnrollmentWatcher(
                worldbrain=self.worldbrain, person_id=self.person_id,
                emit_ui_intent=self._push_intent,
            )

        # ---- StrangerProfiler (E36 §3): provisional VLM description of an ---------
        # unidentified, persistent person → a name-less "? boulanger" hypothesis
        # (truth_level=inferred), fusionnable into a named entity at enrollment.
        self.enable_stranger_profiles = enable_stranger_profiles
        self.stranger_profiler: Any = None
        if enable_stranger_profiles and self.worldbrain is not None:
            scfg = self.profile.get("stranger", {}) if isinstance(self.profile, dict) else {}
            self.stranger_profiler = stranger_profile.StrangerProfiler(
                vlm=self.vision.vlm,
                worldbrain=self.worldbrain,
                config=stranger_profile.StrangerConfig(
                    stable_seconds=float(scfg.get("stable_seconds", 4.0)),
                ),
                on_entity_hot_update=self._push_intent,
            )

        # ---- Fine intelligence (E38): identity hypotheses + bi-modal attribute
        # changes + learned routine→object associations. All generic, all wired to
        # the SAME transcript/OCR/VLM/approach signals — no hardcoded examples.
        self.enable_fine_intel = enable_fine_intel
        self.hypothesis_engine: Any = None
        self.attribute_memory: Any = None
        self.routine_associations: Any = None
        # A generic text-LLM surface for the hypothesis/attribute extractions. The
        # caller may inject a mock/provider; otherwise the local LLM router is used.
        self._fine_intel_llm = fine_intel_llm
        if enable_fine_intel and self.worldbrain is not None:
            hcfg = self.profile.get("fine_intel", {}) if isinstance(self.profile, dict) else {}
            self.hypothesis_engine = hypothesis_engine.HypothesisEngine(
                person_id=self.person_id, llm=fine_intel_llm,
                worldbrain=self.worldbrain, db_path=db_path,
                config=hypothesis_engine.HypothesisConfig(
                    min_occurrences=int(hcfg.get("min_occurrences", 3)),
                    min_cumulative_confidence=float(hcfg.get("min_cumulative_confidence", 1.2)),
                ),
                on_ui_intent=self._push_intent,
            )
            self.attribute_memory = attribute_memory.AttributeMemory(
                person_id=self.person_id, worldbrain=self.worldbrain, llm=fine_intel_llm,
            )
            self.routine_associations = routine_associations.RoutineAssociations(
                person_id=self.person_id, db_path=db_path,
                worldbrain=self.worldbrain, scene_adapter=self.scene_adapter,
            )
            try:
                self.routine_associations.learn()
            except Exception:
                pass

        # ---- IntentRouter (E33): voice + menu → one execution path -----------
        # The general router ABSORBS the enrollment_watcher as one of its handlers
        # (identity commands are pre-routed before the general grammar). The LLM
        # router owns the local<->cloud switch (paid mode) and the parse fallback;
        # memory_query routes "interroge ma mémoire" to the rich Brain2 router.
        self.enable_intents = enable_intents
        self.llm_router: Any = None
        self.memory_query: Any = None
        self.intents: Any = None
        self.vision_focus_handler = vision_focus_handler

        # ---- TTS (E35 §1): short spoken replies when the profile opts in --------
        self.enable_tts = enable_tts
        self.tts: Any = None
        self._tts_on = tts_local.profile_tts_enabled(self.user_profile)
        if enable_tts:
            self.tts = tts_local.build_tts_provider(self.user_profile)

        # ---- Replay (E35 §2): time-range bundle → virtual_screen + timeline -----
        self.enable_replay = enable_replay
        self.replay: Any = None
        if enable_replay:
            self.replay = replay_service_mod.ReplayService(
                person_id=self.person_id, live_session_id=self.session_id,
                db_path=self.db_path, emit_ui_intent=self._push_intent,
            )

        # ---- OwnerSetup (E37 §3): enrol the WEARER's voice → owner attribution ----
        # « configure ma voix » captures the next N wearer segments and enrols them
        # as is_user=True in the SHARED core gallery, so both the night and the E32
        # live matcher recognise the porteur (→ speaker_person_id=owner).
        self.owner_setup: Any = None
        if enable_identity:
            self.owner_setup = owner_setup.OwnerSetup(
                voice_identity=self.voice_identity,
                config=owner_setup.OwnerSetupConfig(person_id=self.person_id),
                emit_ui_intent=self._push_intent,
                db_path=db_path,
            )

        if enable_intents:
            self.llm_router = llm_providers.LLMRouter(
                profile=self.user_profile,
                on_cloud_event=self._push_intent,
            )
            self.memory_query = memory_query.MemoryQuery(person_id=self.person_id)
            self.intents = intent_router.IntentRouter(
                vision_focus=self._route_vision_focus,
                on_device_command=self._push_device_command,
                ask_memory=self.memory_query.ask,
                llm_router=self.llm_router,
                enrollment=self.enrollment,
                emit_ui_intent=self._push_intent,
                replay_service=self.replay,
                owner_setup=self.owner_setup,
                person_id=self.person_id,
            )

        # ---- AudioArchive (E37 §1): live VAD segments → WAV + night speech_segment --
        # Rebuilds the nightly deep-audio input for V19 sessions. Bound + never on the
        # subtitle path. Uses the ConversationBridge's shared live_session_id so the
        # archived audio and the transcript turns land in the same BrainLive session.
        self.enable_audio_archive = enable_audio_archive
        self.audio_archive: Any = None
        # Segment WAV clips keyed by the final subtitle's ui_intent_id, produced by
        # _on_audio_segment and consumed by on_audio_chunk for E32 voice matching.
        self._segment_clips: dict[str, str] = {}
        if enable_audio_archive:
            self.audio_archive = audio_archive.AudioArchive(
                person_id=self.person_id,
                live_session_id=self._archive_session_id(),
                db_path=db_path,
            )

    def _archive_session_id(self) -> str:
        """The BrainLive session id to attach archived audio to.

        Prefer the ConversationBridge's shared live_session_id (same session the
        transcript turns use, so the night assembler folds audio + turns together);
        fall back to the transport session_id when no bridge is wired."""
        if self.conversation is not None:
            try:
                return self.conversation.ensure_session()
            except Exception:
                pass
        return self.session_id

    # ------------------------------------------------------------- push helpers
    def set_status_sink(self, cb: Callable[[dict[str, Any]], Any]) -> None:
        self._status_cb = cb

    # ------------------------------------------------------ outside access (E36 §1)
    def set_active_endpoint(self, name: str | None) -> None:
        """Record which PC endpoint (lan|tunnel|…) this session reached (for /metrics)."""
        self.active_endpoint = name

    def set_active_link(self, link: str) -> None:
        """Switch the network degradation profile at runtime (lan ↔ wan).

        The GPU/heartbeat thresholds are link-independent; only the network latency
        / drop ceilings and the client's target video height follow the link. Called
        when the resolver picks a tunnel endpoint (→ wan) or the LAN (→ lan)."""
        self.active_link = str(link or "lan")
        self.degraded.thresholds = degraded.thresholds_for_link(self.network_profiles, self.active_link)

    def target_video_height(self) -> int:
        """The video height the client should send on the active link (720 lan / 540 wan)."""
        p = self.network_profiles.get(self.active_link) or self.network_profiles["lan"]
        return int(p.target_video_height)

    def resolve_endpoints(self, endpoints: Any = None, *, probe: Any = None) -> dict[str, Any]:
        """Resolve the ordered PC endpoint list (LAN → tunnel), set active endpoint
        + link (tunnel endpoints → wan), and return the resolve result.

        ``endpoints`` defaults to the user profile's ``endpoints:`` list. When
        nothing answers the result is ``pc_unreachable`` and the reflex-only device
        path stays live (no exception)."""
        eps = endpoint_resolver.parse_endpoints(
            endpoints if endpoints is not None else self.user_profile.get("endpoints")
        )
        resolver = endpoint_resolver.EndpointResolver(eps, probe=probe)
        result = resolver.resolve()
        if result.active is not None:
            self.set_active_endpoint(result.active.name)
            # The primary (index 0) endpoint is the LAN; any other → treat as WAN.
            is_primary = bool(eps) and result.active.name == eps[0].name
            self.set_active_link("lan" if is_primary else "wan")
        return result.to_dict()

    def _push_intent(self, intent: dict[str, Any]) -> None:
        if self.ingress is not None and hasattr(self.ingress, "send_ui_intent"):
            try:
                self.ingress.send_ui_intent(json.dumps(intent))
            except Exception:
                pass

    def _push_device_command(self, cmd: dict[str, Any]) -> None:
        """Push a device_command message to Unity over the same DataChannel (E33 §4).

        E35 §1: a ``set_tts`` command is a LOCAL toggle (voice on/off) — applied here
        and still forwarded so the device StatusBar can reflect the state."""
        if isinstance(cmd, dict) and cmd.get("action") == "set_tts":
            self.set_tts(bool(cmd.get("tts")))
        if self.ingress is not None and hasattr(self.ingress, "send_ui_intent"):
            try:
                self.ingress.send_ui_intent(json.dumps(cmd))
            except Exception:
                pass

    # ------------------------------------------------------------------ TTS (E35 §1)
    def set_tts(self, on: bool) -> None:
        """Toggle spoken replies at runtime (« réponds à voix haute » / « silence »
        via an intent or the menu device_command). Independent of the profile default."""
        self._tts_on = bool(on)

    def speak_reply(self, text: str, *, lang: str = "fr", force: bool = False) -> dict[str, Any] | None:
        """Synthesise a short reply and push it as a bounded ``tts_audio`` message.

        Only speaks when TTS is enabled AND (the profile/toggle is on OR ``force``).
        Returns the pushed message, or None (disabled / no provider / too long /
        synthesis unavailable → silent, the ContextCard already carries the text)."""
        if not self.enable_tts or self.tts is None:
            return None
        if not (self._tts_on or force):
            return None
        text = (text or "").strip()
        if not text:
            return None
        try:
            wav = self.tts.speak(text, lang=lang)
        except Exception:
            return None
        msg = tts_local.tts_audio_message(wav, lang=lang, text=text)
        if msg is None:  # too long to fit the DataChannel budget → text card only
            return None
        self._push_intent(msg)
        return msg

    def _route_vision_focus(self, request: dict[str, Any]) -> Any:
        """Bridge a router vision intent (what_is/find/ocr/zoom) to the vision handler.

        A handler injected by the pipeline owner (which holds the current frame)
        takes precedence; otherwise there is no frame here, so return None (the
        router still records the target for multi-turn deixis)."""
        if self.vision_focus_handler is not None:
            return self.vision_focus_handler(request)
        return None

    def _on_scene_delta(self, delta: dict[str, Any]) -> None:
        # Push to device (DataChannel) and mirror to WorldBrain (E28) callback.
        if self.ingress is not None and hasattr(self.ingress, "send_ui_intent"):
            try:
                self.ingress.send_ui_intent(json.dumps({"type": "scene_delta", **delta}))
            except Exception:
                pass
        if self.worldbrain is not None:
            try:
                self.worldbrain.ingest_scene_delta(delta)
            except Exception:
                pass
        # E38 §3: approaching a zone/entity whose learned routine implies an object
        # → proactively push that object's last-seen (deduped per session).
        if self.enable_fine_intel and self.routine_associations is not None:
            try:
                place = self._current_place_subject()
                if place:
                    visible = [e.get("label") for e in (delta.get("entities") or [])]
                    self.routine_associations.on_approach(place_key=place, visible_labels=visible)
            except Exception:
                pass
        if self._external_scene_cb is not None:
            try:
                self._external_scene_cb(delta)
            except Exception:
                pass

    # ------------------------------------------------------------------ degraded
    def update_degraded(self, signals: Any) -> dict[str, Any]:
        state = self.degraded.evaluate(signals)
        if state.action_level != self._last_action:
            self._last_action = state.action_level
            self.vision.apply_action_level(state.action_level)
            if self._status_cb is not None:
                try:
                    self._status_cb(state.event())
                except Exception:
                    pass
        return state.event()

    # ------------------------------------------------------------------- feeders
    def on_video_frame(self, frame_bgr: np.ndarray, envelope: Any, *, focus_active: bool = False, now: float | None = None) -> dict[str, Any] | None:
        # OrientationGuard (E29 §3a): un-rotate to upright BEFORE any processing so
        # detector/OCR see the scene the right way up in capture-only mode.
        if self.apply_rotation:
            rot = int(getattr(envelope, "rotation", 0) or 0) % 360
            if rot:
                frame_bgr = deorient_frame(frame_bgr, rot)
                self.rotation_corrections += 1
        # Feed the spatial provider a pose keyframe (E28) before vision runs.
        # E37 §5: a placeholder envelope (gateway._placeholder_envelope) carries a
        # synthetic neutral pose (0,0,0) flagged pose_valid=False. Never feed it to
        # the spatial map — a fake (0,0,0) would pollute the zone cloud and bearings.
        if self.spatial is not None and bool(getattr(envelope, "pose_valid", True)):
            pose = getattr(envelope, "pose", None)
            if pose is not None:
                try:
                    pd = pose.model_dump() if hasattr(pose, "model_dump") else dict(pose) if isinstance(pose, dict) else {"position": getattr(pose, "position", None), "rotation": getattr(pose, "rotation", None)}
                    self.spatial.observe_pose(getattr(envelope, "frame_id", "?"), pd)
                except Exception:
                    pass
        delta = self.vision.process_frame(frame_bgr, envelope, focus_active=focus_active, now=now)
        # Identity (E32): face-embed person crops at an economical cadence — on a
        # newly-seen person track, or every ``identity_frame_interval`` deltas.
        if delta is not None and self.fusion is not None and self.face is not None:
            self._frame_counter += 1
            try:
                self._run_face_identity(frame_bgr, delta)
            except Exception:
                pass
        # StrangerProfiler (E36 §3): a persistent, still-anonymous person track →
        # one VLM description (name-less hypothesis). Runs after identity so a
        # freshly-named track is skipped.
        if delta is not None and self.stranger_profiler is not None:
            try:
                self._run_stranger_profiles(frame_bgr, delta)
            except Exception:
                pass
        return delta

    def _maybe_fuse_stranger(self, text: str) -> None:
        """After an enrollment command, fuse the active track's provisional profile
        into the named entity (E36 §3 — description kept as an attribute)."""
        if self.stranger_profiler is None or self.enrollment is None:
            return
        try:
            cmd = enrollment_watcher.parse_identity_command(text)
        except Exception:
            cmd = None
        if not cmd or cmd.get("intent") != "enroll":
            return
        name = cmd.get("name")
        if not name:
            return
        track_id = getattr(self.enrollment, "_active_track", None)
        entity_id = getattr(self.enrollment, "_active_entity", None)
        person_id = enrollment_watcher._person_id_for(name)
        try:
            self.stranger_profiler.fuse_into_named(
                track_id=track_id, entity_id=entity_id, person_id=person_id, name=name,
            )
        except Exception:
            pass

    def _maybe_break_hypotheses(self, text: str) -> None:
        """A voice identity correction breaks the active entity's hypotheses (E38 §1)."""
        if not self.enable_fine_intel or self.hypothesis_engine is None:
            return
        try:
            cmd = enrollment_watcher.parse_identity_command(text)
        except Exception:
            cmd = None
        if not cmd or cmd.get("intent") != "correct":
            return
        entity_id = getattr(self.enrollment, "_active_entity", None) if self.enrollment is not None else None
        if entity_id:
            try:
                self.hypothesis_engine.break_hypotheses_for_entity(entity_id)
            except Exception:
                pass

    def _run_stranger_profiles(self, frame_bgr: np.ndarray, delta: dict[str, Any]) -> None:
        """Feed each visible person track to the StrangerProfiler (E36 §3)."""
        named_tracks = set(getattr(self.fusion, "_track_identity", {}) if self.fusion is not None else {})
        for ent in delta.get("entities") or []:
            if ent.get("label") != "person":
                continue
            track_id = str(ent.get("track_id") or "")
            if not track_id:
                continue
            entity_id = self._person_entity_id(track_id)
            is_named = track_id in named_tracks
            crop = None
            if not is_named:
                bbox = ent.get("bbox")
                crop = self.vision._crop(frame_bgr, bbox) if bbox else None
                if crop is not None and getattr(crop, "size", 0) == 0:
                    crop = None
            profile = self.stranger_profiler.observe_track(
                track_id, entity_id=entity_id, is_person=True,
                is_named=is_named, crop_bgr=crop,
            )
            # E38 §2: a fresh VLM appearance descriptor is stored as attribute
            # observations of the person entity → inter-session diff = attribute_changed
            # (same mechanism as any other attribute; no special person path).
            if (self.enable_fine_intel and self.attribute_memory is not None
                    and profile is not None and entity_id):
                try:
                    self.attribute_memory.observe_person_appearance(
                        entity_id=entity_id, descriptor=getattr(profile, "attributes", {}) or {},
                        session=self._brainlive_session_id() or self.session_id,
                        evidence_ref=f"vlm:{track_id}",
                    )
                except Exception:
                    pass

    def _person_entity_id(self, track_id: str) -> str | None:
        """Best-effort WorldBrain entity_id for a person track (if promoted)."""
        if self.worldbrain is None:
            return None
        return self.worldbrain._track_to_entity.get(track_id)  # type: ignore[attr-defined]

    # --------------------------------------------------------------- fine intel (E38)
    def _present_person_entities(self) -> list[str]:
        """Confirmed person entity_ids currently in the world picture."""
        if self.worldbrain is None:
            return []
        out: list[str] = []
        for eid, ent in getattr(self.worldbrain, "entities", {}).items():
            if getattr(ent, "label", None) == "person" and getattr(ent, "lifecycle", None) == "confirmed":
                out.append(eid)
        return out

    def _current_place_subject(self) -> str | None:
        """Stable subject key for the current place/zone (attribute subject)."""
        if self.worldbrain is None:
            return None
        sess = getattr(self.worldbrain, "session", None)
        if sess is None:
            return None
        return getattr(sess, "active_zone", None) or getattr(sess, "place_hint", None)

    def _note_fine_intel_turn(self, text: str, content: dict[str, Any]) -> None:
        if not self.enable_fine_intel:
            return
        session = self._brainlive_session_id() or self.session_id
        speaker_entity = self._speaker_entity_for(content)
        if self.hypothesis_engine is not None:
            try:
                self.hypothesis_engine.note_turn(
                    text, session=session, speaker_entity=speaker_entity,
                    present_person_entities=self._present_person_entities(),
                )
            except Exception:
                pass
        if self.attribute_memory is not None:
            try:
                self.attribute_memory.note_turn(
                    text, session=session,
                    subject_resolver=self._resolve_attribute_subject,
                    default_subject=self._current_place_subject(),
                    evidence_ref=f"turn:{content.get('event_id') or ''}",
                )
            except Exception:
                pass

    def _speaker_entity_for(self, content: dict[str, Any]) -> str | None:
        """Map the turn's speaker (voice-matched person_id) to a present person
        entity, best-effort. When unknown, None (the heuristic still works off the
        previous speaker / single present person)."""
        pid = content.get("speaker_person_id")
        if not pid or self.worldbrain is None:
            return None
        for eid, ent in getattr(self.worldbrain, "entities", {}).items():
            if getattr(ent, "person_id", None) == pid:
                return eid
        return None

    def _resolve_attribute_subject(self, subject_hint: str | None) -> str | None:
        """Map a free subject hint from the heard-fact extraction to a stable key:
        a matching visible entity label → its entity_id; else the current place."""
        if subject_hint and self.worldbrain is not None and hasattr(self.worldbrain, "find_entity"):
            try:
                ent = self.worldbrain.find_entity(subject_hint)
                if ent is not None:
                    return ent.entity_id
            except Exception:
                pass
        return None

    def _brainlive_session_id(self) -> str | None:
        conv = self.conversation
        return getattr(conv, "live_session_id", None) if conv is not None else None

    def _run_face_identity(self, frame_bgr: np.ndarray, delta: dict[str, Any]) -> None:
        periodic = (self._frame_counter % self.identity_frame_interval) == 0
        for ent in delta.get("entities") or []:
            if ent.get("label") != "person":
                continue
            track_id = str(ent.get("track_id") or "")
            if not track_id:
                continue
            new_track = track_id not in self._identity_seen_tracks
            if not (new_track or periodic):
                continue
            self._identity_seen_tracks.add(track_id)
            bbox = ent.get("bbox")
            crop = self.vision._crop(frame_bgr, bbox) if bbox else None
            if crop is None or getattr(crop, "size", 0) == 0:
                continue
            entity_id = self._person_entity_id(track_id)
            try:
                face_res = self.face.match(crop)
            except Exception:
                face_res = None
            # Keep the freshest crop for enrollment ("retiens : c'est X").
            if self.enrollment is not None:
                self.enrollment.set_active_track(track_id, entity_id, crop)
            self.fusion.resolve(entity_id=entity_id, track_id=track_id, face=face_res)

    def _on_audio_segment(self, seg: np.ndarray, meta: dict[str, Any]) -> None:
        """Handle one FINAL raw VAD segment (E37 §1/§3), off the subtitle path.

        Fired by AudioRT AFTER the subtitle intent is emitted. It (1) writes a WAV
        clip for E32 voice matching + owner capture, (2) archives the segment for the
        nightly deep-audio pass (bounded), and (3) feeds an armed owner enrolment.
        Everything here is best-effort and swallows its own errors — the reflex
        subtitle path already returned before this runs."""
        uiid = meta.get("ui_intent_id")
        abs_start = meta.get("absolute_start")
        abs_end = meta.get("absolute_end")

        # (1) Persist a WAV clip so E32 voice matching / owner capture have a file.
        wav_clip: str | None = None
        if self.voice_identity is not None or self.owner_setup is not None or self.audio_archive is not None:
            try:
                import tempfile
                fd = tempfile.NamedTemporaryFile(prefix="seg_", suffix=".wav", delete=False)
                fd.close()
                audio_archive.write_segment_wav(fd.name, seg, sample_rate=int(meta.get("sample_rate", 16000)))
                wav_clip = fd.name
            except Exception:
                wav_clip = None
        if uiid and wav_clip:
            # Picked up in on_audio_chunk's intent loop for voice matching (E32).
            self._segment_clips[str(uiid)] = wav_clip

        # (2) Archive the segment for the night (bounded, non-blocking).
        if self.audio_archive is not None:
            try:
                self.audio_archive.archive_segment(
                    seg,
                    absolute_start=abs_start,
                    absolute_end=abs_end,
                    source_event_id=str(uiid) if uiid else None,
                    transcript_text=meta.get("text"),
                )
            except Exception:
                pass

        # (3) Owner enrolment (E37 §3): while armed, the wearer's next segments enrol
        # their voice as is_user=True. Runs on the clip we just wrote.
        if self.owner_setup is not None and wav_clip:
            try:
                self.owner_setup.offer_segment(wav_clip)
            except Exception:
                pass

    def on_audio_chunk(self, samples: np.ndarray, src_rate: int) -> list[dict[str, Any]]:
        intents = self.audio.push_audio(samples, src_rate)
        # Feed final transcripts two ways: (1) as scene conversation context for
        # the E28 scene adapter, and (2) into the V18.8 conversational loop via
        # the E31 ConversationBridge (turn buffer -> policy -> H1 -> queue).
        for it in intents:
            content = it.get("content") if isinstance(it, dict) else None
            if not (isinstance(content, dict) and content.get("final") and content.get("text")):
                continue
            text = str(content.get("text"))
            if self.scene_adapter is not None:
                try:
                    self.scene_adapter.note_transcript(text)
                except Exception:
                    pass
            # E38 §1/§2: feed the final turn to the fine-intelligence engines — the
            # addressed-name hypothesis signal and the heard attribute-fact signal.
            self._note_fine_intel_turn(text, content)
            # E34 §4: fine discourse analysis off the hot path (background worker).
            if self.live_discourse is not None:
                try:
                    self.live_discourse.note_turn(text, speaker_label=content.get("speaker_label"))
                except Exception:
                    pass
            # Identity (E32): the enrollment_watcher pre-routes "retiens : c'est X"
            # / "non ce n'est pas X" BEFORE conversation ingestion (a device
            # command, not a memory turn). Voice matching sets the speaker on the
            # bridge turn (speaker_person_id / speaker_label) when a wav clip is
            # available on the intent.
            wav_path = content.get("audio_path") or content.get("wav_path")
            if not wav_path:
                # E37 §1: the WAV clip written by _on_audio_segment for this final.
                wav_path = self._segment_clips.pop(str(it.get("ui_intent_id") or ""), None)
            if self.voice_identity is not None and wav_path:
                try:
                    if self.enrollment is not None:
                        self.enrollment.set_active_segment(wav_path)
                    vres = self.voice_identity.match(wav_path)
                    if vres.get("matched") and self.fusion is not None:
                        self.fusion.resolve(track_id=None, voice=vres)
                        # E37 §3: the wearer (owner) enrolled is_user=True is matched
                        # here → speaker_person_id=owner on this turn.
                        content["speaker_person_id"] = vres.get("person_id")
                        content["speaker_label"] = vres.get("name")
                except Exception:
                    pass
            # E33: the IntentRouter is the single entry for final transcripts; it
            # ABSORBS the enrollment_watcher (identity commands are pre-routed
            # inside it). When intents are disabled, fall back to the standalone
            # enrollment watcher so E32 behaviour is preserved verbatim.
            if self.intents is not None:
                try:
                    self.intents.on_transcript(text)
                except Exception:
                    pass
            elif self.enrollment is not None:
                try:
                    self.enrollment.on_transcript(text)
                except Exception:
                    pass
            # E36 §3: if this transcript was an enrollment, fold any provisional
            # stranger profile of the active person track into the now-named entity.
            self._maybe_fuse_stranger(text)
            # E38 §1: a voice identity correction ("non, ce n'est pas X") breaks the
            # active entity's identity hypotheses (the deduction was wrong).
            self._maybe_break_hypotheses(text)
            if self.conversation is not None:
                try:
                    self.conversation.ingest_segment(
                        text,
                        language=content.get("language"),
                        is_final=True,
                        event_id=it.get("ui_intent_id"),
                        speaker_label=content.get("speaker_label"),
                        speaker_person_id=content.get("speaker_person_id"),
                    )
                except Exception:
                    pass
        return intents

    def end_session(self, *, place_hint: str | None = None) -> str | None:
        """Flush the WorldBrain end-of-session summary (E28) + discourse (E34)."""
        if self.live_discourse is not None:
            try:
                self.live_discourse.close()
            except Exception:
                pass
        if self.worldbrain is None:
            return None
        try:
            return self.worldbrain.end_session(place_hint=place_hint)
        except Exception:
            return None

    def deliver_morning_briefing(self, *, force: bool = False) -> dict[str, Any] | None:
        """Deliver the first-session-of-the-day briefing card (E34 §6). Safe to
        call once at session start; dedups naturally on ``briefing:<date>``."""
        if self.morning_briefing is None:
            return None
        try:
            return self.morning_briefing.maybe_deliver(force=force)
        except Exception:
            return None

    def on_focus_request(self, request: dict[str, Any], frame_bgr: np.ndarray, envelope: Any) -> dict[str, Any]:
        # Same orientation guard on focus (what_is/find/ocr) crops (E29 §3a).
        if self.apply_rotation:
            rot = int(getattr(envelope, "rotation", 0) or 0) % 360
            if rot:
                frame_bgr = deorient_frame(frame_bgr, rot)
        result = self.vision.handle_focus(request, frame_bgr, envelope)
        # E38 §2: an OCR reading is an attribute observation of the current place —
        # fed to the bi-modal attribute memory so a re-visit surfaces a change.
        if self.enable_fine_intel and self.attribute_memory is not None and isinstance(result, dict):
            try:
                content = result.get("content") or {}
                subject = self._current_place_subject()
                if content.get("kind") == "ocr" and content.get("lines") and subject:
                    self.attribute_memory.observe_ocr(
                        subject=subject, readings=content.get("lines"),
                        session=self._brainlive_session_id() or self.session_id,
                        evidence_ref=f"frame:{getattr(envelope, 'frame_id', '?')}",
                    )
            except Exception:
                pass
        return result

    async def run_video(self, *, limit: int | None = None) -> dict[str, Any]:
        """Consume the ingress and drive VisionRT until it stops."""
        assert self.ingress is not None, "ingress required for run_video"
        count = 0
        async for frame_bgr, envelope in self.ingress:
            self.on_video_frame(frame_bgr, envelope)
            count += 1
            if limit is not None and count >= limit:
                break
        return self.metrics()

    # ------------------------------------------------------------------- metrics
    def metrics(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "session_id": self.session_id,
            "action_level": self._last_action,
            "display": self.display,
            "rotation_corrections": self.rotation_corrections,
            # E36 §1: which PC endpoint this session reached the PC through and the
            # active network link (lan|wan) driving the network thresholds.
            "active_endpoint": self.active_endpoint,
            "active_link": self.active_link,
            "target_video_height": self.target_video_height(),
        }
        m.update(self.vision.metrics.snapshot())
        m.update({f"audio_{k}": v for k, v in self.audio.metrics.snapshot().items()})
        if self.worldbrain is not None:
            wm = self.worldbrain.metrics
            m["map_quality"] = round(self.worldbrain.session.map_quality, 3)
            m["last_seen_count"] = wm.get("last_seen_count", 0)
            m["change_events"] = wm.get("change_events", 0)
            m["entities_promoted"] = wm.get("entities_promoted", 0)
        if self.scene_adapter is not None:
            m["hot_context_builds"] = self.scene_adapter.metrics.get("hot_context_builds", 0)
            m["deliveries_enqueued"] = self.scene_adapter.metrics.get("deliveries_enqueued", 0)
        if self.conversation is not None:
            cm = self.conversation.metrics
            m["conversation_turns"] = cm.get("conversation_turns", 0)
            m["h1_candidates"] = cm.get("h1_candidates", 0)
            m["hot_cycles"] = cm.get("hot_cycles", 0)
        if self.fusion is not None:
            fm = self.fusion.metrics
            m["identity_matches"] = fm.get("identity_matches", 0)
            m["named_entities"] = fm.get("named_entities", 0)
            m["identity_contradictions"] = fm.get("contradictions", 0)
        if self.enrollment is not None:
            em = self.enrollment.metrics
            m["enrollments"] = em.get("enrollments", 0)
            m["corrections"] = em.get("corrections", 0)
        if self.face is not None:
            m["face_matches"] = self.face.metrics.get("matches", 0)
        if self.intents is not None:
            im = self.intents.metrics
            m["intents_routed"] = im.get("intents_routed", 0)
            m["intent_unknown"] = im.get("intent_unknown", 0)
            m["grammar_hits"] = im.get("grammar_hits", 0)
            m["multiturn_hits"] = im.get("multiturn_hits", 0)
            m["llm_fallbacks"] = im.get("llm_fallbacks", 0)
        if self.llm_router is not None:
            m["cloud_mode"] = self.llm_router.mode
            m["cloud_active"] = self.llm_router.cloud_active
        if self.scene_adapter is not None and self.enable_proactivity:
            sm = self.scene_adapter.metrics
            m["proactive_predictions"] = sm.get("proactive_predictions", 0)
            m["proactive_interventions"] = sm.get("proactive_interventions", 0)
            m["clarifications_asked"] = sm.get("clarifications_asked", 0)
            m["similar_experiences"] = sm.get("similar_experiences", 0)
            m["entity_hot_updates"] = sm.get("entity_hot_updates", 0)
        if self.live_discourse is not None:
            dm = self.live_discourse.metrics
            m["discourse_turns"] = dm.get("turns_seen", 0)
            m["discourse_flushes"] = dm.get("flushes", 0)
        if self.morning_briefing is not None:
            m["briefings_enqueued"] = self.morning_briefing.metrics.get("briefings_enqueued", 0)
        if self.stranger_profiler is not None:
            spm = self.stranger_profiler.metrics
            m["stranger_profiles"] = spm.get("profiles_created", 0)
            m["stranger_vlm_unavailable"] = spm.get("vlm_unavailable", 0)
            m["stranger_fused"] = spm.get("fused", 0)
        # E38 §4: fine-intelligence metrics.
        if self.hypothesis_engine is not None:
            hm = self.hypothesis_engine.metrics
            m["hypotheses_active"] = len(self.hypothesis_engine.active_hypotheses())
            m["auto_promotions"] = hm.get("auto_promotions", 0)
            m["clarifications_resolved"] = hm.get("clarifications_resolved", 0)
        if self.attribute_memory is not None:
            m["attribute_changes"] = self.attribute_memory.metrics.get("attribute_changes", 0)
        if self.routine_associations is not None:
            m["routine_pushes"] = self.routine_associations.metrics.get("routine_pushes", 0)
        if self.ingress is not None and hasattr(self.ingress, "matcher"):
            try:
                m["envelope_match"] = self.ingress.matcher.stats()
            except Exception:
                pass
        return m


def create_metrics_app(pipeline: LivePipeline):
    """A tiny FastAPI app exposing GET /metrics for the pipeline (E27 §3)."""
    from fastapi import FastAPI

    app = FastAPI(title="MLOmega V19 LivePipeline metrics")

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        return pipeline.metrics()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "detector": pipeline.detector_available,
            "asr": pipeline.audio.transcriber.available,
        }

    return app
