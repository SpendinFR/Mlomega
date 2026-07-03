from __future__ import annotations

"""Conversation-level discourse context for millimetric memory ingestion.

V3.2.2 made each utterance precise. V3.2.3 adds the missing global layer:
what is the whole conversation about, which topic threads persist across time,
which utterances refer back to earlier utterances, and whether the same subject
runs through the full audio.

This is intentionally strict: no heuristic topic mapper and no silent fallback.
If the local LLM cannot produce the map, ingestion stops instead of pretending
that local context is enough.
"""

import json
from dataclasses import dataclass
from typing import Any

from .config import get_settings
from .db import upsert
from .llm import EliteLLMError, OllamaJsonClient
from .memory_foundation import add_memory_card, add_memory_facet, add_memory_link, TRUTH_INFERRED, TRUTH_CONSOLIDATED
from .utils import iso_add_seconds, json_dumps, now_iso, sha256_bytes, stable_id

DISCOURSE_SCHEMA_VERSION = "3.2.3-global-discourse-context"


class ConversationDiscourseError(RuntimeError):
    pass


@dataclass
class ConversationDiscourse:
    raw: dict[str, Any]
    conversation_summary: str
    primary_subject: str
    subject_is_stable: bool
    emotional_arc: str
    intent_arc: str
    conversation_tone: str
    unresolved_questions: list[Any]
    topic_threads: list[dict[str, Any]]
    utterance_contexts: list[dict[str, Any]]
    callbacks: list[dict[str, Any]]
    turning_points: list[dict[str, Any]]

    def context_for_turn(self, idx: int) -> dict[str, Any]:
        matches = [u for u in self.utterance_contexts if int(u.get("turn_idx", -1)) == idx]
        if not matches:
            raise ConversationDiscourseError(f"Carte globale incomplète: aucun contexte discursif pour turn_idx={idx}")
        ctx = matches[0]
        active_keys = {str(k) for k in ctx.get("active_thread_keys", [])}
        active_threads = [t for t in self.topic_threads if str(t.get("thread_key")) in active_keys]
        callbacks_in = [c for c in self.callbacks if int(c.get("to_turn_idx", -9999)) == idx]
        callbacks_out = [c for c in self.callbacks if int(c.get("from_turn_idx", -9999)) == idx]
        return {
            "conversation_summary": self.conversation_summary,
            "primary_subject": self.primary_subject,
            "subject_is_stable": self.subject_is_stable,
            "emotional_arc": self.emotional_arc,
            "intent_arc": self.intent_arc,
            "conversation_tone": self.conversation_tone,
            "current_utterance_discourse": ctx,
            "active_topic_threads": active_threads,
            "callbacks_to_current": callbacks_in,
            "callbacks_from_current": callbacks_out,
            "turning_points_near_current": [tp for tp in self.turning_points if abs(int(tp.get("turn_idx", -9999)) - idx) <= 1],
            "global_instruction": (
                "Utiliser cette carte pour comprendre les retours de sujet, références distantes, "
                "sujet unique sur toute la conversation et continuité émotionnelle. Ne pas inventer: "
                "les souvenirs de current_turn doivent rester ancrés dans la preuve exacte du tour courant."
            ),
        }


def _compact_transcript(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, t in enumerate(turns):
        rows.append({
            "turn_idx": idx,
            "speaker": t.get("person_id") or t.get("speaker") or t.get("speaker_label"),
            "start_s": t.get("start"),
            "end_s": t.get("end"),
            "text": t.get("text", ""),
        })
    return rows


class ConversationDiscourseAnalyzer:
    REQUIRED_KEYS = {
        "conversation_summary",
        "primary_subject",
        "subject_is_stable",
        "conversation_arc",
        "topic_threads",
        "utterance_contexts",
        "callbacks",
        "turning_points",
    }

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.enable_llm_deep:
            raise ConversationDiscourseError("MLOMEGA_ENABLE_LLM_DEEP=false refusé: la carte globale exige le LLM local.")
        self.llm = OllamaJsonClient()

    def analyze(self, *, turns: list[dict[str, Any]], topic: str | None, participants: list[str], relationship_context: dict[str, Any] | None) -> ConversationDiscourse:
        if not turns:
            raise ConversationDiscourseError("Impossible de construire une carte globale: aucune utterance.")
        system = (
            "Tu es le Global Conversation Cartographer de MemoryLight Omega. "
            "Tu lis toute la conversation AVANT l'analyse au millimètre des utterances. "
            "Ta tâche: construire une carte discursive globale: sujet principal, fils thématiques, arcs émotionnels/intentionnels, "
            "références distantes, retours à un sujet évoqué plus tôt, et contexte spécifique de chaque utterance. "
            "Tu ne fais pas encore la mémoire profonde de chaque phrase: tu fournis le contexte global strict pour que le microscope ne perde jamais le fil. "
            "Réponds en JSON strict sans markdown. Chaque turn_idx du transcript doit apparaître exactement une fois dans utterance_contexts."
        )
        schema = {
            "conversation_summary": "résumé global dense de la conversation",
            "primary_subject": "sujet principal canonique",
            "subject_is_stable": True,
            "conversation_arc": {
                "opening": "string",
                "middle": "string",
                "ending": "string",
                "emotional_arc": "string",
                "intent_arc": "string",
                "conversation_tone": "ton global: calme|tendu|enthousiaste|intime|argumentatif|exploratoire|autre + nuance",
                "unresolved_questions": ["string"],
            },
            "topic_threads": [
                {
                    "thread_key": "stable short key, e.g. projet_ia",
                    "label": "nom lisible du fil",
                    "summary": "ce que ce fil veut dire dans la conversation",
                    "life_domain": "project|relationship|identity|work|health|money|family|learning|emotion|other",
                    "status": "opened|continued|resolved|unresolved|returned_later|background",
                    "importance": 0.0,
                    "start_turn_idx": 0,
                    "end_turn_idx": 0,
                    "start_s": 0.0,
                    "end_s": 0.0,
                    "participants": ["person_id"],
                }
            ],
            "utterance_contexts": [
                {
                    "turn_idx": 0,
                    "local_subject": "sujet exact de cette utterance",
                    "active_thread_keys": ["thread_key"],
                    "relation_to_previous": "continues|shifts|answers|contradicts|returns_to_earlier|clarifies|opens_new_thread",
                    "context_summary": "pourquoi cette utterance existe dans le fil global",
                    "emotional_continuity": "continuité ou rupture émotionnelle",
                    "unresolved_references": ["pronoms, ça, ce truc, référence implicite"],
                    "confidence": 0.0,
                }
            ],
            "turning_points": [
                {
                    "turn_idx": 0,
                    "turning_point_type": "topic_shift|emotional_shift|decision_point|contradiction|clarification|commitment|realization",
                    "summary": "moment où la conversation bascule ou révèle une tension/prise de position",
                    "before_state": "état avant",
                    "after_state": "état après",
                    "evidence_text": "court extrait exact",
                    "confidence": 0.0
                }
            ],
            "callbacks": [
                {
                    "from_turn_idx": 0,
                    "to_turn_idx": 10,
                    "thread_key": "thread_key",
                    "relation_type": "returns_to|answers|contradicts|confirms|reframes|continues|depends_on",
                    "summary": "la fin revient sur ou dépend du début",
                    "evidence_text": "court extrait exact si possible",
                    "confidence": 0.0,
                }
            ],
        }
        payload = {
            "topic_hint": topic,
            "participants": participants,
            "relationship_context": relationship_context or {},
            "transcript": _compact_transcript(turns),
            "critical_instructions": [
                "Détecte si toute la conversation garde le même sujet ou si elle change de fil.",
                "Détecte les retours tardifs: une phrase à la fin qui répond, confirme, contredit ou reformule une idée du début.",
                "Détecte les moments de bascule: changement de ton, décision, contradiction, prise de conscience, engagement ou pivot de sujet.",
                "Chaque utterance doit avoir active_thread_keys, même si le sujet est identique pendant tout l'audio.",
                "N'analyse pas encore chaque mot: fournis le contexte global utilisable par le microscope.",
            ],
        }
        try:
            raw = self.llm.require_json(system, json.dumps(payload, ensure_ascii=False), schema_hint=schema, timeout=120)
        except EliteLLMError as exc:
            raise ConversationDiscourseError(f"Carte globale LLM indisponible: {exc}") from exc
        return self._normalize(raw, expected_turn_count=len(turns))

    def _normalize(self, raw: dict[str, Any], *, expected_turn_count: int) -> ConversationDiscourse:
        missing = [k for k in self.REQUIRED_KEYS if k not in raw]
        if missing:
            raise ConversationDiscourseError(f"Carte globale incomplète: champs manquants {missing}")
        topic_threads = raw.get("topic_threads")
        utterance_contexts = raw.get("utterance_contexts")
        callbacks = raw.get("callbacks")
        turning_points = raw.get("turning_points")
        if not isinstance(topic_threads, list) or not topic_threads:
            raise ConversationDiscourseError("Carte globale invalide: topic_threads vide ou absent")
        if not isinstance(utterance_contexts, list):
            raise ConversationDiscourseError("Carte globale invalide: utterance_contexts doit être une liste")
        if not isinstance(callbacks, list):
            raise ConversationDiscourseError("Carte globale invalide: callbacks doit être une liste")
        if not isinstance(turning_points, list):
            raise ConversationDiscourseError("Carte globale invalide: turning_points doit être une liste")
        seen = []
        normalized_contexts: list[dict[str, Any]] = []
        for u in utterance_contexts:
            if not isinstance(u, dict):
                continue
            try:
                idx = int(u.get("turn_idx"))
            except Exception:
                raise ConversationDiscourseError(f"utterance_context sans turn_idx entier: {u}")
            seen.append(idx)
            active = u.get("active_thread_keys") or []
            if not isinstance(active, list) or not active:
                raise ConversationDiscourseError(f"turn_idx={idx} sans active_thread_keys")
            normalized_contexts.append({
                "turn_idx": idx,
                "local_subject": str(u.get("local_subject") or "conversation"),
                "active_thread_keys": [str(x) for x in active if str(x).strip()],
                "relation_to_previous": str(u.get("relation_to_previous") or "continues"),
                "context_summary": str(u.get("context_summary") or "contexte discursif global"),
                "emotional_continuity": str(u.get("emotional_continuity") or "unknown"),
                "unresolved_references": u.get("unresolved_references") if isinstance(u.get("unresolved_references"), list) else [],
                "confidence": float(u.get("confidence", 0.7) or 0.7),
                "raw": u,
            })
        expected = set(range(expected_turn_count))
        if set(seen) != expected:
            missing_idxs = sorted(expected - set(seen))
            extra_idxs = sorted(set(seen) - expected)
            raise ConversationDiscourseError(f"Carte globale non alignée: missing_turn_idx={missing_idxs}, extra_turn_idx={extra_idxs}")
        normalized_threads: list[dict[str, Any]] = []
        for t in topic_threads:
            if not isinstance(t, dict):
                continue
            key = str(t.get("thread_key") or t.get("label") or "thread").strip()
            label = str(t.get("label") or key).strip()
            summary = str(t.get("summary") or label).strip()
            if not key or not label or not summary:
                continue
            normalized_threads.append({
                "thread_key": key,
                "label": label,
                "summary": summary,
                "life_domain": str(t.get("life_domain") or "other"),
                "status": str(t.get("status") or "opened"),
                "importance": float(t.get("importance", 0.7) or 0.7),
                "start_turn_idx": int(t.get("start_turn_idx", 0) or 0),
                "end_turn_idx": int(t.get("end_turn_idx", expected_turn_count - 1) or expected_turn_count - 1),
                "start_s": t.get("start_s"),
                "end_s": t.get("end_s"),
                "participants": t.get("participants") if isinstance(t.get("participants"), list) else [],
                "raw": t,
            })
        keys = {t["thread_key"] for t in normalized_threads}
        missing_threads = sorted({k for u in normalized_contexts for k in u["active_thread_keys"] if k not in keys})
        if missing_threads:
            raise ConversationDiscourseError(f"active_thread_keys inconnues: {missing_threads}")
        arc = raw.get("conversation_arc") if isinstance(raw.get("conversation_arc"), dict) else {}
        normalized_callbacks: list[dict[str, Any]] = []
        for c in callbacks:
            if not isinstance(c, dict):
                continue
            try:
                from_idx = int(c.get("from_turn_idx"))
                to_idx = int(c.get("to_turn_idx"))
            except Exception:
                continue
            if from_idx not in expected or to_idx not in expected:
                continue
            normalized_callbacks.append({
                "from_turn_idx": from_idx,
                "to_turn_idx": to_idx,
                "thread_key": str(c.get("thread_key") or ""),
                "relation_type": str(c.get("relation_type") or "related_to"),
                "summary": str(c.get("summary") or "référence distante"),
                "evidence_text": str(c.get("evidence_text") or ""),
                "confidence": float(c.get("confidence", 0.7) or 0.7),
                "raw": c,
            })
        normalized_turning_points: list[dict[str, Any]] = []
        for tp in turning_points:
            if not isinstance(tp, dict):
                continue
            try:
                idx = int(tp.get("turn_idx"))
            except Exception:
                continue
            if idx not in expected:
                continue
            summary = str(tp.get("summary") or "").strip()
            if not summary:
                continue
            normalized_turning_points.append({
                "turn_idx": idx,
                "turning_point_type": str(tp.get("turning_point_type") or "turning_point"),
                "summary": summary,
                "before_state": str(tp.get("before_state") or ""),
                "after_state": str(tp.get("after_state") or ""),
                "evidence_text": str(tp.get("evidence_text") or ""),
                "confidence": float(tp.get("confidence", 0.7) or 0.7),
                "raw": tp,
            })
        return ConversationDiscourse(
            raw=raw,
            conversation_summary=str(raw.get("conversation_summary") or ""),
            primary_subject=str(raw.get("primary_subject") or "conversation"),
            subject_is_stable=bool(raw.get("subject_is_stable")),
            emotional_arc=str(arc.get("emotional_arc") or ""),
            intent_arc=str(arc.get("intent_arc") or ""),
            conversation_tone=str(arc.get("conversation_tone") or raw.get("conversation_tone") or ""),
            unresolved_questions=arc.get("unresolved_questions") if isinstance(arc.get("unresolved_questions"), list) else [],
            topic_threads=normalized_threads,
            utterance_contexts=sorted(normalized_contexts, key=lambda x: x["turn_idx"]),
            callbacks=normalized_callbacks,
            turning_points=normalized_turning_points,
        )


def discourse_prompt_hash(*, turns: list[dict[str, Any]], topic: str | None, participants: list[str], relationship_context: dict[str, Any] | None) -> str:
    payload = {
        "topic": topic,
        "participants": participants,
        "relationship_context": relationship_context or {},
        "transcript": _compact_transcript(turns),
        "schema_version": DISCOURSE_SCHEMA_VERSION,
    }
    return sha256_bytes(json_dumps(payload).encode("utf-8"))


def store_conversation_discourse(
    con,
    *,
    conversation_id: str,
    discourse: ConversationDiscourse,
    extraction_run_id: str,
    turn_ids_by_idx: dict[int, str],
    started_at: str | None,
    turn_times_by_idx: dict[int, tuple[str | None, str | None]] | None = None,
) -> dict[str, Any]:
    """Persist the global discourse map and expose it as memory cards/facets."""
    now = now_iso()
    turn_times_by_idx = turn_times_by_idx or {}

    def turn_time(turn_idx: int | None) -> tuple[str | None, str | None]:
        if turn_idx is None:
            return started_at, None
        return turn_times_by_idx.get(int(turn_idx), (started_at, None))

    def offset_time(offset_s: Any) -> str | None:
        return iso_add_seconds(started_at, offset_s) if offset_s is not None else started_at

    discourse_id = stable_id("discourse", conversation_id, discourse.primary_subject, DISCOURSE_SCHEMA_VERSION)
    upsert(con, "conversation_discourse_maps", {
        "discourse_id": discourse_id,
        "conversation_id": conversation_id,
        "primary_subject": discourse.primary_subject,
        "subject_is_stable": 1 if discourse.subject_is_stable else 0,
        "conversation_summary": discourse.conversation_summary,
        "emotional_arc": discourse.emotional_arc,
        "intent_arc": discourse.intent_arc,
        "unresolved_questions_json": json_dumps(discourse.unresolved_questions),
        "discourse_json": json_dumps(discourse.raw),
        "extraction_run_id": extraction_run_id,
        "created_at": now,
    }, "discourse_id")
    discourse_card_id = add_memory_card(
        con,
        source_table="conversation_discourse_maps",
        source_id=discourse_id,
        card_type="conversation_discourse_map",
        truth_status=TRUTH_CONSOLIDATED,
        title=f"Carte globale: {discourse.primary_subject}",
        summary=discourse.conversation_summary,
        person_id=None,
        topic=discourse.primary_subject,
        time_start=started_at,
        confidence=0.9,
        extraction_run_id=extraction_run_id,
        metadata={
            "subject_is_stable": discourse.subject_is_stable,
            "emotional_arc": discourse.emotional_arc,
            "intent_arc": discourse.intent_arc,
            "conversation_tone": discourse.conversation_tone,
            "unresolved_questions": discourse.unresolved_questions,
        },
    )
    add_memory_facet(con, target_table="memory_cards", target_id=discourse_card_id, facet_type="global_context", facet_value="conversation_discourse", source="system", confidence=1.0)
    add_memory_facet(con, target_table="memory_cards", target_id=discourse_card_id, facet_type="primary_subject", facet_value=discourse.primary_subject, source="llm", confidence=0.9)
    add_memory_facet(con, target_table="memory_cards", target_id=discourse_card_id, facet_type="subject_is_stable", facet_value=str(discourse.subject_is_stable).lower(), source="llm", confidence=0.9)
    if discourse.conversation_tone:
        add_memory_facet(con, target_table="memory_cards", target_id=discourse_card_id, facet_type="conversation_tone", facet_value=discourse.conversation_tone, source="llm", confidence=0.85)

    thread_ids: dict[str, str] = {}
    thread_card_ids: dict[str, str] = {}
    for t in discourse.topic_threads:
        thread_id = stable_id("thread", conversation_id, t["thread_key"])
        thread_ids[t["thread_key"]] = thread_id
        thread_abs_start = offset_time(t.get("start_s"))
        thread_abs_end = iso_add_seconds(started_at, t.get("end_s")) if t.get("end_s") is not None else None
        upsert(con, "conversation_topic_threads", {
            "thread_id": thread_id,
            "conversation_id": conversation_id,
            "thread_key": t["thread_key"],
            "label": t["label"],
            "summary": t["summary"],
            "life_domain": t["life_domain"],
            "status": t["status"],
            "importance": t["importance"],
            "start_turn_idx": t["start_turn_idx"],
            "end_turn_idx": t["end_turn_idx"],
            "start_s": t.get("start_s"),
            "end_s": t.get("end_s"),
            "participants_json": json_dumps(t.get("participants", [])),
            "metadata_json": json_dumps({"absolute_start": thread_abs_start, "absolute_end": thread_abs_end, "raw": t.get("raw", t)}),
            "extraction_run_id": extraction_run_id,
            "created_at": now,
        }, "thread_id")
        card_id = add_memory_card(
            con,
            source_table="conversation_topic_threads",
            source_id=thread_id,
            card_type="conversation_topic_thread",
            truth_status=TRUTH_CONSOLIDATED,
            title=f"Fil conversationnel: {t['label']}",
            summary=t["summary"],
            person_id=None,
            topic=t["label"],
            time_start=thread_abs_start,
            time_end=thread_abs_end,
            confidence=t["importance"],
            extraction_run_id=extraction_run_id,
            metadata=t,
        )
        thread_card_ids[t["thread_key"]] = card_id
        add_memory_link(con, from_table="memory_cards", from_id=discourse_card_id, relation_type="contains_topic_thread", to_table="conversation_topic_threads", to_id=thread_id, confidence=t["importance"], extraction_run_id=extraction_run_id)
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="life_domain", facet_value=t["life_domain"], source="llm", confidence=t["importance"])
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="thread_status", facet_value=t["status"], source="llm", confidence=t["importance"])

    utterance_link_count = 0
    for u in discourse.utterance_contexts:
        turn_idx = int(u["turn_idx"])
        turn_id = turn_ids_by_idx[turn_idx]
        turn_card = con.execute("SELECT card_id FROM memory_cards WHERE source_table='turns' AND source_id=? LIMIT 1", (turn_id,)).fetchone()
        turn_card_id = turn_card["card_id"] if turn_card else None
        for key in u["active_thread_keys"]:
            thread_id = thread_ids.get(key)
            link_id = stable_id("udlink", conversation_id, turn_id, key)
            upsert(con, "utterance_discourse_links", {
                "link_id": link_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "turn_idx": turn_idx,
                "thread_id": thread_id,
                "thread_key": key,
                "local_subject": u["local_subject"],
                "relation_to_previous": u["relation_to_previous"],
                "context_summary": u["context_summary"],
                "emotional_continuity": u["emotional_continuity"],
                "unresolved_references_json": json_dumps(u["unresolved_references"]),
                "confidence": u["confidence"],
                "extraction_run_id": extraction_run_id,
                "metadata_json": json_dumps(u.get("raw", u)),
                "created_at": now,
            }, "link_id")
            if turn_card_id:
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="discourse_thread", facet_value=key, source="global_discourse", confidence=u["confidence"])
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="local_subject", facet_value=u["local_subject"], source="global_discourse", confidence=u["confidence"])
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="relation_to_previous", facet_value=u["relation_to_previous"], source="global_discourse", confidence=u["confidence"])
                if thread_id:
                    add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="belongs_to_topic_thread", to_table="conversation_topic_threads", to_id=thread_id, confidence=u["confidence"], extraction_run_id=extraction_run_id, metadata={"thread_key": key, "turn_idx": turn_idx})
            utterance_link_count += 1

    turning_point_count = 0
    for tp in discourse.turning_points:
        turn_idx = int(tp["turn_idx"])
        turn_id = turn_ids_by_idx.get(turn_idx)
        tp_id = stable_id("turningpoint", conversation_id, turn_idx, tp["turning_point_type"], tp["summary"])
        tp_abs_start, tp_abs_end = turn_time(turn_idx)
        upsert(con, "conversation_turning_points", {
            "turning_point_id": tp_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "turn_idx": turn_idx,
            "turning_point_type": tp["turning_point_type"],
            "summary": tp["summary"],
            "before_state": tp.get("before_state"),
            "after_state": tp.get("after_state"),
            "evidence_text": tp.get("evidence_text"),
            "confidence": tp["confidence"],
            "extraction_run_id": extraction_run_id,
            "metadata_json": json_dumps({"absolute_start": tp_abs_start, "absolute_end": tp_abs_end, "raw": tp.get("raw", tp)}),
            "created_at": now,
        }, "turning_point_id")
        tp_card_id = add_memory_card(
            con,
            source_table="conversation_turning_points",
            source_id=tp_id,
            card_type="conversation_turning_point",
            truth_status=TRUTH_INFERRED,
            title=f"Moment de bascule: {tp['turning_point_type']}",
            summary=tp["summary"],
            person_id=None,
            topic=discourse.primary_subject,
            time_start=tp_abs_start,
            time_end=tp_abs_end,
            confidence=tp["confidence"],
            extraction_run_id=extraction_run_id,
            metadata=tp,
        )
        add_memory_link(con, from_table="memory_cards", from_id=discourse_card_id, relation_type="contains_turning_point", to_table="conversation_turning_points", to_id=tp_id, confidence=tp["confidence"], extraction_run_id=extraction_run_id)
        if turn_id:
            turn_card = con.execute("SELECT card_id FROM memory_cards WHERE source_table='turns' AND source_id=? LIMIT 1", (turn_id,)).fetchone()
            if turn_card:
                add_memory_link(con, from_table="memory_cards", from_id=turn_card["card_id"], relation_type="is_turning_point", to_table="memory_cards", to_id=tp_card_id, confidence=tp["confidence"], extraction_run_id=extraction_run_id)
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card["card_id"], facet_type="turning_point_type", facet_value=tp["turning_point_type"], source="global_discourse", confidence=tp["confidence"])
        turning_point_count += 1

    callback_count = 0
    for c in discourse.callbacks:
        thread_id = thread_ids.get(c.get("thread_key"))
        callback_id = stable_id("callback", conversation_id, c["from_turn_idx"], c["to_turn_idx"], c["relation_type"], c["summary"])
        callback_abs_start, _ = turn_time(int(c["from_turn_idx"]))
        _, callback_abs_end = turn_time(int(c["to_turn_idx"]))
        upsert(con, "conversation_callbacks", {
            "callback_id": callback_id,
            "conversation_id": conversation_id,
            "from_turn_id": turn_ids_by_idx.get(int(c["from_turn_idx"])),
            "to_turn_id": turn_ids_by_idx.get(int(c["to_turn_idx"])),
            "from_turn_idx": int(c["from_turn_idx"]),
            "to_turn_idx": int(c["to_turn_idx"]),
            "thread_id": thread_id,
            "thread_key": c.get("thread_key"),
            "relation_type": c["relation_type"],
            "summary": c["summary"],
            "evidence_text": c["evidence_text"],
            "confidence": c["confidence"],
            "extraction_run_id": extraction_run_id,
            "metadata_json": json_dumps({"absolute_start": callback_abs_start, "absolute_end": callback_abs_end, "raw": c.get("raw", c)}),
            "created_at": now,
        }, "callback_id")
        callback_card_id = add_memory_card(
            con,
            source_table="conversation_callbacks",
            source_id=callback_id,
            card_type="conversation_callback",
            truth_status=TRUTH_INFERRED,
            title=f"Référence distante: {c['relation_type']}",
            summary=c["summary"],
            person_id=None,
            topic=c.get("thread_key") or discourse.primary_subject,
            time_start=callback_abs_start,
            time_end=callback_abs_end,
            confidence=c["confidence"],
            extraction_run_id=extraction_run_id,
            metadata=c,
        )
        add_memory_link(con, from_table="memory_cards", from_id=discourse_card_id, relation_type="contains_callback", to_table="conversation_callbacks", to_id=callback_id, confidence=c["confidence"], extraction_run_id=extraction_run_id)
        if c.get("thread_key") in thread_card_ids:
            add_memory_link(con, from_table="memory_cards", from_id=thread_card_ids[c["thread_key"]], relation_type="has_callback", to_table="memory_cards", to_id=callback_card_id, confidence=c["confidence"], extraction_run_id=extraction_run_id)
        callback_count += 1
    return {
        "discourse_id": discourse_id,
        "discourse_card_id": discourse_card_id,
        "thread_ids": thread_ids,
        "thread_card_ids": thread_card_ids,
        "topic_threads": len(thread_ids),
        "utterance_discourse_links": utterance_link_count,
        "callbacks": callback_count,
        "turning_points": turning_point_count,
    }
