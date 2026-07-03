from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import get_settings
from .llm import EliteLLMError, OllamaJsonClient
from .utils import normalize_text
from .llm_contracts_v15_18 import normalize_salient_words, normalize_personal_language_items, clamp


class EliteMicroscopeError(RuntimeError):
    pass


@dataclass
class TurnAnalysis:
    words: list[dict[str, Any]]
    expressions: list[dict[str, Any]]
    utterance: dict[str, Any]
    ideas: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    commitments: list[dict[str, Any]]
    memory_frames: list[dict[str, Any]]
    memory_facets: list[dict[str, Any]]
    llm_raw: dict[str, Any]
    memory_action: dict[str, Any] = field(default_factory=lambda: {"memory_action": "store", "signal_type": "unknown", "reason": "legacy/default", "confidence": 0.5})
    personal_language_items: list[dict[str, Any]] = field(default_factory=list)


class ConversationMicroscope:
    """Strict LLM conversation microscope.

    The previous deterministic/regex analyst has been removed from this elite
    build. Every word signal, expression, intent, decision and commitment must be
    produced by the configured local LLM and validated before ingestion continues.
    """

    REQUIRED_UTTERANCE = {
        "surface_meaning",
        "deep_intent",
        "emotion",
        "emotion_intensity",
        "why_now",
        "trigger_summary",
        "hidden_expectation",
        "response_rule",
        "confidence",
    }

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.enable_llm_deep:
            raise EliteMicroscopeError("MLOMEGA_ENABLE_LLM_DEEP=false refusé: le microscope élite exige l'analyse LLM profonde.")
        self.llm = OllamaJsonClient()

    def analyze_turn(self, text: str, previous: str | None, topic: str | None, speaker: str | None, relationship_context: dict | None = None, context_window: dict[str, Any] | None = None, discourse_context: dict[str, Any] | None = None) -> TurnAnalysis:
        route = self._llm_memory_router(text, previous, topic, speaker, relationship_context or {}, context_window or {}, discourse_context or {})
        memory_action = self._normalize_memory_action(route)
        if memory_action["memory_action"] in {"ignore", "watch"} and memory_action["signal_type"] in {"filler", "smalltalk", "unknown"} and memory_action["confidence"] >= 0.65:
            data = self._minimal_turn_analysis(text, memory_action)
        else:
            data = self._llm_turn_analysis(text, previous, topic, speaker, relationship_context or {}, context_window or {}, discourse_context or {}, memory_action)
        utterance = self._require_utterance(data)
        return TurnAnalysis(
            words=normalize_salient_words(data.get("salient_words"), turn_text=text),
            expressions=self._normalize_expressions(self._require_list(data, "expressions"), text),
            utterance=utterance,
            ideas=self._normalize_ideas(self._require_list(data, "ideas"), text, topic),
            decisions=self._normalize_decisions(self._require_list(data, "decisions")),
            commitments=self._normalize_commitments(self._require_list(data, "commitments"), speaker),
            memory_frames=self._normalize_memory_frames(self._require_list(data, "memory_frames"), text, topic, speaker),
            memory_facets=self._normalize_memory_facets(self._require_list(data, "memory_facets")),
            llm_raw={**data, "v15_18_memory_router": route},
            memory_action=memory_action,
            personal_language_items=normalize_personal_language_items(data.get("personal_language_items") or data.get("language_items"), fallback_text=text),
        )


    def _llm_memory_router(self, text: str, previous: str | None, topic: str | None, speaker: str | None, relationship_context: dict, context_window: dict[str, Any], discourse_context: dict[str, Any]) -> dict[str, Any]:
        system = (
            "Tu es le routeur mémoire V15.18. Décide si current_turn mérite une analyse profonde. "
            "Ne fais aucune conclusion psychologique; classe seulement le type de signal et l'action mémoire. "
            "Réponds JSON strict."
        )
        schema = {
            "memory_action": "store|watch|ignore",
            "signal_type": "fact|expression|emotion|commitment|correction|question|choice|filler|smalltalk|unknown",
            "reason": "string",
            "confidence": 0.0,
        }
        payload = {
            "current_turn": text,
            "previous_turn": previous,
            "topic": topic,
            "speaker": speaker,
            "relationship_context": relationship_context,
            "context_window": context_window,
            "discourse_context": discourse_context,
            "instruction": "Si c'est une expression personnelle utile mais pas un fait, renvoie memory_action=store signal_type=expression. Si c'est du remplissage, watch ou ignore. Si correction utilisateur, store/correction.",
        }
        try:
            return self.llm.require_json(system, json.dumps(payload, ensure_ascii=False), schema_hint=schema, timeout=45)
        except EliteLLMError as exc:
            raise EliteMicroscopeError(f"Routeur mémoire LLM indisponible: {exc}") from exc

    def _normalize_memory_action(self, data: dict[str, Any]) -> dict[str, Any]:
        action = str(data.get("memory_action") or "store").lower()
        if action not in {"store", "watch", "ignore"}:
            action = "store"
        signal = str(data.get("signal_type") or "unknown").lower()
        reason = str(data.get("reason") or "routeur mémoire V15.18")
        return {"memory_action": action, "signal_type": signal, "reason": reason, "confidence": clamp(data.get("confidence"), default=0.5)}

    def _minimal_turn_analysis(self, text: str, memory_action: dict[str, Any]) -> dict[str, Any]:
        return {
            "surface_meaning": text,
            "deep_intent": "signal faible; analyse profonde non déclenchée",
            "emotion": "unknown",
            "emotion_intensity": 0.0,
            "why_now": memory_action.get("reason"),
            "trigger_summary": memory_action.get("signal_type"),
            "hidden_expectation": "non inféré",
            "response_rule": "ne pas surinterpréter; garder comme contexte léger",
            "confidence": memory_action.get("confidence", 0.5),
            "salient_words": [],
            "expressions": [],
            "ideas": [],
            "decisions": [],
            "commitments": [],
            "memory_frames": [],
            "memory_facets": [{"facet_type": "memory_action", "facet_value": memory_action.get("memory_action"), "confidence": memory_action.get("confidence", 0.5), "weight": 1.0}],
            "personal_language_items": [],
        }

    def _llm_turn_analysis(self, text: str, previous: str | None, topic: str | None, speaker: str | None, relationship_context: dict, context_window: dict[str, Any], discourse_context: dict[str, Any], memory_action: dict[str, Any] | None = None) -> dict[str, Any]:
        system = (
            "Tu es le Conversation Microscope de MemoryLight Omega. Analyse une utterance atomique au millimètre. "
            "Cette utterance vient d'un découpage word-level/pause/punctuation; elle peut être courte, hésitante ou dépendre du contexte. "
            "Tu dois relier mots, timing, contexte local, carte globale de conversation, sujet, intention, émotion, réaction, attente cachée, idée, décision, pattern. "
            "Utilise before/after et discourse_context pour désambiguïser les retours de sujet distants; n'attribue les frames et souvenirs qu'à current_turn. "
            "Tu dois produire une mémoire exploitable: frames typées + facettes de classement, avec preuve textuelle exacte tirée de current_turn. "
            "Interdiction d'inventer: chaque champ doit s'appuyer sur current_turn, le contexte local, le tour précédent, le contexte relationnel ou la carte globale. "
            "Réponds en JSON strict, sans markdown."
        )
        schema = {
            "surface_meaning": "string",
            "deep_intent": "string",
            "emotion": "string",
            "emotion_intensity": 0.0,
            "why_now": "string",
            "trigger_summary": "string",
            "hidden_expectation": "string",
            "response_rule": "string",
            "confidence": 0.0,
            "memory_action": "store|watch|ignore",
            "signal_type": "fact|expression|emotion|commitment|correction|question|choice|filler|smalltalk|unknown",
            "salient_words": [{"token":"string","position":0,"role":"string","why_it_matters":"string","salience":0.0}],
            "personal_language_items": [{"text":"string","meaning":"string","tone":"string","contexts":[],"response_implication":"string","do_not_overpsychologize":True,"evidence_turn_ids":[],"confidence":0.0}],
            "expressions": [{"expression":"string","category":"string","personal_meaning":"string","why_now":"string","response_rule":"string","intensity":0.0,"evidence_text":"string","do_not_overpsychologize":True}],
            "ideas": [{"canonical_topic":"string","idea_text":"string","stance":"string","novelty":0.0,"importance":0.0,"evidence_text":"string"}],
            "decisions": [{"decision_text":"string","rationale":"string","confidence":0.0}],
            "commitments": [{"promised_by":"string","promised_to":"string","content":"string","status":"open","evidence_text":"string","confidence":0.0}],
            "memory_frames": [
                {
                    "frame_type": "choice|action|plan|belief|desire|fear|constraint|need|boundary|relationship_signal|identity_signal|contradiction_signal|question|request|expense|location|health|work|social|error",
                    "summary": "string",
                    "actor_person_id": "string",
                    "target": "string",
                    "topic": "string",
                    "polarity": "positive|negative|ambivalent|neutral",
                    "temporal_status": "past|present|future|habitual|hypothetical",
                    "evidence_text": "string",
                    "confidence": 0.0
                }
            ],
            "memory_facets": [
                {
                    "facet_type": "life_domain|project|person|emotion|need|value|risk|decision_area|relationship_dynamic|time_horizon|energy_state|communication_style|location|money|health_state|social_context|place|habit|mistake_area",
                    "facet_value": "string",
                    "confidence": 0.0,
                    "weight": 1.0
                }
            ],
        }
        prompt = {
            "speaker": speaker,
            "topic": topic,
            "relationship_context": relationship_context,
            "previous_turn": previous,
            "context_window": context_window,
            "discourse_context": discourse_context,
            "current_turn": text,
            "memory_router_decision": memory_action or {},
            "critical_instruction": "Analyse current_turn mot par mot seulement si memory_router_decision le justifie. Sépare expression personnelle et fait. Ne transforme jamais une expression en conclusion psychologique sans preuves exactes. Utilise discourse_context pour relier une phrase de fin à un sujet du début ou reconnaître un sujet global identique, mais ne mélange pas les intentions des autres tours avec celles de current_turn.",
            "project_line": "Une IA qui connaît la continuité personnelle, voit les patterns cachés, et s'adapte à la vie réelle.",
            "life_stream_instruction": "Traite chaque utterance comme une source de vie: décisions, erreurs, dépenses, lieux, repas, relations, santé, travail, émotions, contraintes et événements racontés doivent devenir des frames/facets exploitables si le texte les justifie.",
        }
        try:
            return self.llm.require_json(system, json.dumps(prompt, ensure_ascii=False), schema_hint=schema, timeout=90)
        except EliteLLMError as exc:
            raise EliteMicroscopeError(f"Analyse LLM profonde indisponible: {exc}") from exc

    def _require_utterance(self, data: dict[str, Any]) -> dict[str, Any]:
        missing = [k for k in self.REQUIRED_UTTERANCE if k not in data]
        if missing:
            raise EliteMicroscopeError(f"Réponse LLM incomplète: champs manquants {missing}")
        out = {k: data[k] for k in self.REQUIRED_UTTERANCE}
        out["surface_meaning"] = str(out["surface_meaning"])
        out["deep_intent"] = str(out["deep_intent"])
        out["emotion"] = str(out["emotion"])
        out["why_now"] = str(out["why_now"])
        out["trigger_summary"] = str(out["trigger_summary"])
        out["hidden_expectation"] = str(out["hidden_expectation"])
        out["response_rule"] = str(out["response_rule"])
        out["emotion_intensity"] = float(out["emotion_intensity"] or 0.0)
        out["confidence"] = float(out["confidence"] or 0.0)
        return out

    def _require_list(self, data: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = data.get(key)
        if value is None:
            raise EliteMicroscopeError(f"Réponse LLM incomplète: liste '{key}' absente")
        if not isinstance(value, list):
            raise EliteMicroscopeError(f"Réponse LLM invalide: '{key}' doit être une liste")
        return [x for x in value if isinstance(x, dict)]

    def _normalize_expressions(self, rows: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        out = []
        for e in rows:
            expr = str(e.get("expression", "")).strip()
            if not expr:
                continue
            out.append({
                "expression": expr,
                "category": str(e.get("category") or "llm_expression"),
                "personal_meaning": str(e.get("personal_meaning") or "expression significative selon le LLM local"),
                "why_now": str(e.get("why_now") or "déduit par le LLM local à partir du contexte"),
                "response_rule": str(e.get("response_rule") or "tenir compte du signal dans les futures réponses"),
                "intensity": float(e.get("intensity", 0.75) or 0.75),
                "evidence_text": str(e.get("evidence_text") or text),
                "do_not_overpsychologize": bool(e.get("do_not_overpsychologize", True)),
                "memory_use_policy": "style_context_only",
            })
        return out

    def _normalize_ideas(self, rows: list[dict[str, Any]], text: str, topic: str | None) -> list[dict[str, Any]]:
        out = []
        for i in rows:
            idea = str(i.get("idea_text", "")).strip()
            if not idea:
                continue
            out.append({
                "canonical_topic": str(i.get("canonical_topic") or topic or "conversation"),
                "idea_text": idea,
                "stance": str(i.get("stance") or "llm_stance"),
                "novelty": float(i.get("novelty", 0.7) or 0.7),
                "importance": float(i.get("importance", 0.75) or 0.75),
                "evidence_text": str(i.get("evidence_text") or text),
            })
        return out

    def _normalize_decisions(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for d in rows:
            decision = str(d.get("decision_text", "")).strip()
            if decision:
                out.append({
                    "decision_text": decision,
                    "rationale": str(d.get("rationale") or "décision détectée par LLM local"),
                    "confidence": float(d.get("confidence", 0.75) or 0.75),
                })
        return out

    def _normalize_commitments(self, rows: list[dict[str, Any]], speaker: str | None) -> list[dict[str, Any]]:
        out = []
        for c in rows:
            content = str(c.get("content", "")).strip()
            if content:
                out.append({
                    "promised_by": str(c.get("promised_by") or speaker or "unknown"),
                    "promised_to": str(c.get("promised_to") or "unknown"),
                    "content": content,
                    "status": str(c.get("status") or "open"),
                    "evidence_text": str(c.get("evidence_text") or content),
                    "confidence": float(c.get("confidence", 0.7) or 0.7),
                })
        return out


    def _normalize_memory_frames(self, rows: list[dict[str, Any]], text: str, topic: str | None, speaker: str | None) -> list[dict[str, Any]]:
        out = []
        for f in rows:
            summary = str(f.get("summary", "")).strip()
            frame_type = str(f.get("frame_type") or f.get("type") or "").strip()
            if not summary or not frame_type:
                continue
            out.append({
                "frame_type": frame_type,
                "summary": summary,
                "actor_person_id": str(f.get("actor_person_id") or speaker or "unknown"),
                "target": str(f.get("target") or "").strip() or None,
                "topic": str(f.get("topic") or topic or "conversation"),
                "polarity": str(f.get("polarity") or "neutral"),
                "temporal_status": str(f.get("temporal_status") or "present"),
                "evidence_text": str(f.get("evidence_text") or text),
                "confidence": float(f.get("confidence", 0.7) or 0.7),
                "raw": f,
            })
        if not out:
            raise EliteMicroscopeError("Réponse LLM invalide: 'memory_frames' ne contient aucun frame exploitable")
        return out

    def _normalize_memory_facets(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for f in rows:
            facet_type = str(f.get("facet_type") or f.get("type") or "").strip()
            facet_value = str(f.get("facet_value") or f.get("value") or "").strip()
            if not facet_type or not facet_value:
                continue
            out.append({
                "facet_type": facet_type,
                "facet_value": facet_value,
                "confidence": float(f.get("confidence", 0.7) or 0.7),
                "weight": float(f.get("weight", 1.0) or 1.0),
                "source": str(f.get("source") or "llm"),
                "raw": f,
            })
        if not out:
            raise EliteMicroscopeError("Réponse LLM invalide: 'memory_facets' ne contient aucune facette exploitable")
        return out
