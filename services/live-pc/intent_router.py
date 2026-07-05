from __future__ import annotations

"""IntentRouter — general voice intent router for the live glasses (E33 §1).

After the wake word, a final transcript arrives here. The router resolves it to
one intent and dispatches to an EXISTING handler — it never duplicates business
logic (vision handlers, spatial find, UI toggles, device actions, memory query,
enrollment all live elsewhere; the router only decides *which* and *with what*).

Resolution order:

1. **Grammar first** (regex/keywords, FR+EN) — fast, deterministic, offline. Covers
   what_is / find(target) / ocr / translate / zoom / hide_all / show_all /
   free_guy / privacy_pause / menu / open(maps|youtube|app) / paid_mode(openai|
   gemini) / local_mode / replay(time) / ask_memory / enroll+correction (delegated
   to the E32 :class:`EnrollmentWatcher`, absorbed here as a handler).
2. **Multi-turn** — a short-TTL context of the last command/target/answer resolves
   deixis ("et ça ?", "zoom dessus", "traduis-le") onto the last track/entity/text.
3. **LLM fallback** — for anything grammar misses, ask the live LLM for strict JSON
   (Ollama if up, else honest "je n'ai pas compris : …" UIIntent).

The router is transport-agnostic: it calls injected handlers and returns a routed
dict. ``emit_ui_intent`` pushes reply/ack UIIntents; ``emit_device_command`` pushes
``device_command`` messages (open_app / set_ui_mode / privacy_pause) to Unity.
"""

import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Multi-turn context lifetime: deixis only resolves shortly after its referent.
_CONTEXT_TTL_S = 25.0

_TARGET = r"([\wÀ-ÖØ-öø-ÿ '\-\.]{1,50})"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# --------------------------------------------------------------------------- grammar
# Each rule: (compiled regex, intent name, group->param mapping or callable).
# Order matters: earlier, more specific rules win. Correction/enroll are matched
# via the EnrollmentWatcher grammar (delegated) BEFORE the general rules so
# "c'est pas X" is never mistaken for a "what_is" query.

def _build_rules() -> list[tuple[re.Pattern[str], str, dict[str, Any]]]:
    I = re.IGNORECASE
    rules: list[tuple[re.Pattern[str], str, dict[str, Any]]] = []

    def add(pattern: str, intent: str, **params: Any) -> None:
        rules.append((re.compile(pattern, I), intent, params))

    # --- device / cloud toggles ---
    add(r"\bmode\s+(?:free\s*guy|freeguy|libre)\b", "set_ui_mode", ui_mode="freeguy")
    add(r"\b(?:cache\s+tout|masque\s+tout|hide\s+(?:all|everything)|tout\s+cacher)\b", "set_ui_mode", ui_mode="hide_all")
    add(r"\b(?:mode\s+)?minimal\b", "set_ui_mode", ui_mode="minimal")
    add(r"\b(?:affiche\s+tout|montre\s+tout|show\s+(?:all|everything)|mode\s+normal)\b", "set_ui_mode", ui_mode="normal")
    add(r"\b(?:pause\s+priv[ée]e?|mode\s+priv[ée]|privacy\s+pause|private\s+mode|pause\s+la\s+cam)\b", "privacy_pause")

    add(r"\bmode\s+payant\b\s*(?:avec\s+)?(openai|gpt|gemini|google)?", "paid_mode")
    add(r"\bpaid\s+mode\b\s*(openai|gpt|gemini|google)?", "paid_mode")
    add(r"\b(?:mode\s+local|retour\s+local|local\s+mode|reviens?\s+en\s+local|mode\s+gratuit)\b", "local_mode")

    # --- menu ---
    add(r"\b(?:ouvre\s+le\s+)?menu\b", "menu")
    add(r"\bopen\s+(?:the\s+)?menu\b", "menu")

    # --- open app (specific first) ---
    add(r"\b(?:ouvre|lance|open|navigate\s+to|va\s+[àa])\b\s+(?:maps|carte|itin[ée]raire|navigation|google\s+maps)\b\s*(?:vers|jusqu'?[àa]|to|for)?\s*" + _TARGET, "open_app", app="maps")
    add(r"\b(?:ouvre|lance|open)\b\s+youtube\b\s*" + _TARGET, "open_app", app="youtube")
    add(r"\b(?:ouvre|lance|open|launch)\b\s+(?:l'?app(?:lication)?\s+)?" + _TARGET, "open_app", app="package")

    # --- vision handlers ---
    add(r"\b(?:c'?est\s+quoi|qu'?est-?ce\s+que\s+c'?est|what\s+is\s+(?:this|that))\b", "what_is")
    add(r"\b(?:lis|lire|ocr|read|d[ée]chiffre)\b(?:\s+(?:le\s+)?texte)?", "ocr")
    add(r"\b(?:trouve|cherche|find|where\s+is|o[ùu]\s+est)\b\s+" + _TARGET, "find")
    add(r"\b(?:zoom|agrandis|agrandir)\b", "zoom")

    # --- translate ---
    add(r"\b(?:traduis|traduire|translate)\b(?:[- ](?:le|la|ça|ca|it|this))?\s*(?:en\s+([\w]+))?", "translate")

    # --- replay ---
    add(r"\b(?:rejoue|replay|revois|montre[- ]moi)\b.*?(\d{1,2}\s*[h:]\s*\d{0,2}|\d{1,2}\s*heures?)", "replay")

    # --- owner voice setup (E37 §3) — BEFORE set_tts: its "parle"/"voix" words
    # would otherwise swallow "c'est moi qui parle" / "configure ma voix".
    add(r"\b(?:configure|enregistre|apprends|m[ée]morise)\s+ma\s+voix\b", "owner_enroll")
    add(r"\bc'?est\s+moi\s+qui\s+parle\b", "owner_enroll")
    add(r"\b(?:set\s*up|record|learn)\s+my\s+voice\b", "owner_enroll")
    add(r"\bit'?s\s+me\s+(?:speaking|talking)\b", "owner_enroll")

    # --- TTS toggle (E35 §1) ---
    add(r"\b(?:r[ée]ponds?\s+[àa]\s+voix\s+haute|parle|lis\s+[àa]\s+voix\s+haute|voix\s+haute|active\s+la\s+voix|speak\s+(?:out\s+)?loud|read\s+aloud|voice\s+on)\b", "set_tts", tts=True)
    add(r"\b(?:tais[- ]toi|silence|mode\s+silencieux|coupe\s+la\s+voix|d[ée]sactive\s+la\s+voix|mute|voice\s+off|be\s+quiet)\b", "set_tts", tts=False)

    # --- memory ---
    add(r"\b(?:interroge\s+ma\s+m[ée]moire|demande\s+[àa]\s+ma\s+m[ée]moire|ask\s+my\s+memory)\b\s*[:,]?\s*(.*)", "ask_memory")
    add(r"\b(?:rappelle[- ]moi|remind\s+me)\b\s*(.*)", "ask_memory")
    add(r"\bqu'?est-?ce\s+que\s+j['e]\s*(.*)", "ask_memory")

    return rules


_RULES = _build_rules()


# --------------------------------------------------------------------------- NL-first
# High-confidence grammar shortcut (E34 §1a): a command that *begins* with an
# exact control keyword is an unambiguous instrument order — routed instantly,
# offline, with no LLM round-trip. Everything else is treated as natural language
# and parsed by the live LLM first (§1b); the full lenient grammar is only a
# safety net when the LLM is unavailable (§1c). The rule is intentionally strict:
# the utterance must START with the keyword (after an optional politeness lead-in),
# so "cache tout" is instant but "tu peux cacher les trucs vers midi ?" goes to the
# LLM where the nuance lives.
_LEADIN = r"(?:s'?il\s+te\s+pla[iî]t\s+|st[pe]\s+|please\s+|hey\s+|ok\s+|dis\s+|allez\s+)?"
_HIGH_CONFIDENCE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_LEADIN + p, re.IGNORECASE), name)
    for p, name in [
        (r"menu\b", "menu"),
        (r"open\s+(?:the\s+)?menu\b", "menu"),
        (r"(?:cache|masque)\s+tout\b", "set_ui_mode"),
        (r"tout\s+cacher\b", "set_ui_mode"),
        (r"hide\s+(?:all|everything)\b", "set_ui_mode"),
        (r"(?:affiche|montre)\s+tout\b", "set_ui_mode"),
        (r"show\s+(?:all|everything)\b", "set_ui_mode"),
        (r"mode\s+(?:free\s*guy|freeguy|libre)\b", "set_ui_mode"),
        (r"(?:mode\s+)?minimal\b", "set_ui_mode"),
        (r"mode\s+normal\b", "set_ui_mode"),
        (r"zoom\b", "zoom"),
        (r"agrandis?\b", "zoom"),
        (r"pause\s+priv[ée]e?\b", "privacy_pause"),
        (r"privacy\s+pause\b", "privacy_pause"),
        (r"private\s+mode\b", "privacy_pause"),
        (r"mode\s+priv[ée]\b", "privacy_pause"),
        (r"mode\s+payant\b", "paid_mode"),
        (r"paid\s+mode\b", "paid_mode"),
        (r"mode\s+local\b", "local_mode"),
        (r"local\s+mode\b", "local_mode"),
        (r"mode\s+gratuit\b", "local_mode"),
        (r"configure\s+ma\s+voix\b", "owner_enroll"),
        (r"c'?est\s+moi\s+qui\s+parle\b", "owner_enroll"),
        (r"set\s*up\s+my\s+voice\b", "owner_enroll"),
    ]
]


def _high_confidence_match(text: str) -> bool:
    """True when the utterance *begins* with an exact control keyword (§1a)."""
    return any(pat.match(text) for pat, _ in _HIGH_CONFIDENCE)

# Deixis: pronouns that resolve on the last target/answer.
_DEIXIS = re.compile(r"\b(?:et\s+)?(?:ça|ca|celui-?l[àa]|celle-?l[àa]|dessus|le|la|it|this|that)\b", re.IGNORECASE)
_ZOOM_DEIXIS = re.compile(r"\bzoom\b|\bagrandis\b", re.IGNORECASE)
_TRANSLATE_DEIXIS = re.compile(r"\btraduis[- ]?(?:le|la|ça|ca)?\b|\btranslate\s+it\b", re.IGNORECASE)
_WHAT_DEIXIS = re.compile(r"\bet\s+(?:ça|ca)\s*\??$|\bet\s+celui-?l[àa]\b", re.IGNORECASE)


# A deictic follow-up: an utterance that leans on the previous target rather than
# naming a new one ("zoom dessus", "traduis-le", "et ça ?", a bare "dessus").
_FOLLOWUP = re.compile(
    r"\bdessus\b|\btraduis[- ]?(?:le|la|ça|ca)\b|\btranslate\s+it\b|"
    r"^\s*et\s+(?:ça|ca)\b|\bcelui-?l[àa]\b|\bcelle-?l[àa]\b|zoom\s+(?:dessus|l[àa])",
    re.IGNORECASE,
)


def _is_deictic_followup(text: str) -> bool:
    return bool(_FOLLOWUP.search(text))


def _clean_target(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip().strip(".,!?;:").strip()
    return t or None


class RoutedIntent(dict):
    """A routed intent result (dict subclass for convenient ``["intent"]`` access)."""


class IntentContext:
    """Short-TTL memory of the last command/target/answer for multi-turn deixis."""

    def __init__(self) -> None:
        self.last_intent: str | None = None
        self.last_track_id: str | None = None
        self.last_entity_id: str | None = None
        self.last_bbox: Any = None
        self.last_text: str | None = None
        self.updated_at: float = 0.0

    def note(self, *, intent: str | None = None, track_id: str | None = None, entity_id: str | None = None, bbox: Any = None, text: str | None = None) -> None:
        if intent:
            self.last_intent = intent
        if track_id is not None:
            self.last_track_id = track_id
        if entity_id is not None:
            self.last_entity_id = entity_id
        if bbox is not None:
            self.last_bbox = bbox
        if text is not None:
            self.last_text = text
        self.updated_at = time.monotonic()

    def fresh(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.updated_at) <= _CONTEXT_TTL_S and self.updated_at > 0

    def target(self) -> dict[str, Any]:
        return {"track_id": self.last_track_id, "entity_id": self.last_entity_id, "bbox": self.last_bbox}


class IntentRouter:
    """General voice intent router. See module docstring.

    Handlers (all optional; a missing handler yields an honest "unhandled" reply):
      * ``vision_focus(request)`` — what_is/find/ocr/zoom on the current focus crop.
      * ``on_device_command(cmd)`` — set_ui_mode/open_app/privacy_pause to device.
      * ``ask_memory(question)`` — MemoryQuery.ask → ContextCard.
      * ``llm_router`` — LLMRouter for paid/local switch + parse fallback.
      * ``enrollment`` — E32 EnrollmentWatcher (absorbed: enroll/correction).
      * ``emit_ui_intent(intent)`` — push a reply/ack UIIntent to the device.
    """

    def __init__(
        self,
        *,
        vision_focus: Callable[[dict[str, Any]], Any] | None = None,
        on_device_command: Callable[[dict[str, Any]], Any] | None = None,
        ask_memory: Callable[[str], dict[str, Any]] | None = None,
        llm_router: Any = None,
        enrollment: Any = None,
        emit_ui_intent: Callable[[dict[str, Any]], Any] | None = None,
        replay_service: Any = None,
        owner_setup: Any = None,
        person_id: str = "me",
    ) -> None:
        self.vision_focus = vision_focus
        self.on_device_command = on_device_command
        self.ask_memory = ask_memory
        self.llm_router = llm_router
        self.enrollment = enrollment
        # E37 §3: owner voice enrolment ("configure ma voix"). Arms the wearer capture.
        self.owner_setup = owner_setup
        self._emit = emit_ui_intent
        # E35: when a ReplayService is wired, ``replay`` assembles a real bundle
        # (keyframes/clips/events/transcript) → virtual_screen + timeline, instead
        # of a bare ``replay`` device_command. Falls back to the device_command
        # path (the phone's own replay UI) when no service is injected.
        self.replay_service = replay_service
        self.person_id = person_id
        self.context = IntentContext()
        self.metrics: dict[str, Any] = {
            "intents_routed": 0,
            "intent_unknown": 0,
            "grammar_hits": 0,
            "multiturn_hits": 0,
            "llm_fallbacks": 0,
        }

    # ---- context feed (pipeline updates the "current focus target") ---------
    def note_focus_target(self, *, track_id: str | None = None, entity_id: str | None = None, bbox: Any = None) -> None:
        self.context.note(track_id=track_id, entity_id=entity_id, bbox=bbox)

    def _ui(self, intent: dict[str, Any]) -> None:
        if self._emit is not None:
            try:
                self._emit(intent)
            except Exception:
                pass

    def _unknown(self, text: str) -> RoutedIntent:
        self.metrics["intent_unknown"] += 1
        intent = {
            "type": "ui_intent",
            "ui_intent_id": str(uuid.uuid4()),
            "producer": "ultralive",
            "component": "context_card",
            "content": {"kind": "unknown_command", "text": f"Je n'ai pas compris : « {text} »"},
            "truth_level": "inferred",
            "confidence": 0.0,
            "priority": 0.4,
            "ttl_ms": 6000,
            "evidence_refs": [],
        }
        self._ui(intent)
        return RoutedIntent(intent="unknown", text=text, ui_intent=intent)

    # ---- main entry ---------------------------------------------------------
    def on_transcript(self, text: str) -> RoutedIntent:
        """Route one final transcript to an intent + handler. Never raises."""
        raw = _norm(text)
        if not raw:
            return RoutedIntent(intent="empty")

        # 1) Identity commands first (absorbed E32 pre-router) — enroll/correction.
        if self.enrollment is not None:
            try:
                ident = self.enrollment.on_transcript(raw)
            except Exception:
                ident = None
            if ident is not None:
                self.metrics["intents_routed"] += 1
                self.metrics["grammar_hits"] += 1
                self.context.note(intent=ident.get("intent"))
                return RoutedIntent(intent=ident.get("intent"), params=ident, handled=True)

        # 2) Multi-turn deixis first when the utterance is clearly a follow-up
        # ("zoom dessus", "traduis-le", "et ça ?") and a fresh target exists —
        # otherwise a bare "zoom"/"traduis" grammar match would drop the referent.
        if self.context.fresh() and _is_deictic_followup(raw):
            routed = self._match_deixis(raw)
            if routed is not None:
                self.metrics["multiturn_hits"] += 1
                return self._dispatch(routed, raw)

        # 3) High-confidence grammar shortcut (§1a): only when the utterance BEGINS
        # with an exact control keyword ("menu", "cache tout", "zoom", "mode payant"
        # …). These are unambiguous instrument orders — instant, offline, no LLM.
        if _high_confidence_match(raw):
            routed = self._match_grammar(raw)
            if routed is not None:
                self.metrics["grammar_hits"] += 1
                return self._dispatch(routed, raw)

        # 4) Natural language first (§1b): everything else goes to the live LLM,
        # which parses free speech ("tu peux me montrer ce que j'ai fait vers 14h ?"
        # → replay_at) into one strict-JSON intent. This is the primary path.
        routed = self._llm_parse(raw)
        if routed is not None:
            self.metrics["llm_fallbacks"] += 1
            return self._dispatch(routed, raw)

        # 5) Lenient grammar NET (§1c): only reached when the LLM is unavailable
        # (offline / not configured). The full regex grammar still resolves the
        # common orders so the glasses stay useful without any model.
        routed = self._match_grammar(raw)
        if routed is not None:
            self.metrics["grammar_hits"] += 1
            return self._dispatch(routed, raw)

        # 6) Multi-turn deixis (general), as a final referent-based resolution.
        routed = self._match_deixis(raw)
        if routed is not None:
            self.metrics["multiturn_hits"] += 1
            return self._dispatch(routed, raw)

        return self._unknown(raw)

    # ---- resolution ---------------------------------------------------------
    def _match_grammar(self, text: str) -> dict[str, Any] | None:
        for pat, intent, params in _RULES:
            m = pat.search(text)
            if not m:
                continue
            out: dict[str, Any] = {"intent": intent, **params}
            if intent == "find":
                out["query"] = _clean_target(m.group(1))
                if not out["query"]:
                    continue
            elif intent == "open_app":
                if params.get("app") == "maps":
                    out["destination"] = _clean_target(m.group(1))
                elif params.get("app") == "youtube":
                    out["query"] = _clean_target(m.group(1))
                else:
                    out["package"] = _clean_target(m.group(1))
                    if not out["package"]:
                        continue
            elif intent == "paid_mode":
                prov = (m.group(1) or "").lower()
                out["provider"] = "gemini" if prov in ("gemini", "google") else "openai"
            elif intent == "translate":
                lang = m.group(1)
                if lang:
                    out["language"] = lang.lower()
            elif intent == "replay":
                out["time"] = _clean_target(m.group(1))
            elif intent == "ask_memory":
                q = _clean_target(m.group(1)) if m.groups() else None
                out["question"] = q or text
            return out
        return None

    def _match_deixis(self, text: str) -> dict[str, Any] | None:
        if not self.context.fresh():
            return None
        if not _DEIXIS.search(text):
            return None
        tgt = self.context.target()
        if _ZOOM_DEIXIS.search(text):
            return {"intent": "zoom", **tgt, "deixis": True}
        if _TRANSLATE_DEIXIS.search(text):
            return {"intent": "translate", **tgt, "deixis": True}
        if _WHAT_DEIXIS.search(text) or text.strip().lower() in ("et ça ?", "et ça", "et ca"):
            return {"intent": "what_is", **tgt, "deixis": True}
        # Bare deixis with a live target → repeat the last vision query on it.
        if self.context.last_intent in ("what_is", "find", "ocr", "zoom"):
            return {"intent": self.context.last_intent, **tgt, "deixis": True}
        return None

    def _llm_parse(self, text: str) -> dict[str, Any] | None:
        if self.llm_router is None:
            return None
        schema = {
            "intent": "one of: what_is|find|ocr|translate|zoom|set_ui_mode|privacy_pause|"
                      "open_app|paid_mode|local_mode|menu|replay|ask_memory|owner_enroll|unknown",
            "query": "string (target for find, or search text for open_app youtube)",
            "ui_mode": "hide_all|minimal|normal|freeguy",
            "app": "maps|youtube|package",
            "destination": "string (place for open_app maps)",
            "package": "string (android package for open_app)",
            "language": "string (target language for translate)",
            "provider": "openai|gemini (for paid_mode)",
            "time": "string, an hour like '14h' or '14h30' (for replay)",
            "question": "string (the memory question for ask_memory)",
        }
        system = (
            "Tu es le routeur d'intentions de lunettes AR. L'utilisateur parle NATURELLEMENT, "
            "pas en commandes ; comprends l'intention réelle derrière la phrase et classe-la en "
            "UN intent de la liste, puis extrais ses paramètres. Réponds en JSON strict. "
            "Exemples : « tu peux me montrer ce que j'ai fait vers 14h ? » -> "
            "{\"intent\":\"replay\",\"time\":\"14h\"} ; « c'est quoi ce truc devant moi ? » -> "
            "{\"intent\":\"what_is\"} ; « emmène-moi à la gare » -> "
            "{\"intent\":\"open_app\",\"app\":\"maps\",\"destination\":\"la gare\"} ; "
            "« qu'est-ce que j'avais promis à Sarah déjà ? » -> "
            "{\"intent\":\"ask_memory\",\"question\":\"qu'est-ce que j'avais promis à Sarah\"} ; "
            "« configure ma voix » / « c'est moi qui parle » -> "
            "{\"intent\":\"owner_enroll\"}. "
            "Si aucun intent ne correspond, intent=unknown."
        )
        try:
            data = self.llm_router.complete_json(system, text, schema_hint=schema, timeout=8)
        except Exception:
            return None
        intent = str(data.get("intent") or "unknown").strip().lower()
        if intent in ("", "unknown"):
            return None
        out: dict[str, Any] = {"intent": intent, "llm": True}
        for k in ("query", "ui_mode", "app", "language", "provider", "question", "package", "destination", "time"):
            if data.get(k):
                out[k] = data[k]
        if intent == "ask_memory" and "question" not in out:
            out["question"] = text
        return out

    # ---- dispatch (route to existing handlers only) -------------------------
    def _dispatch(self, routed: dict[str, Any], text: str) -> RoutedIntent:
        intent = routed["intent"]
        self.metrics["intents_routed"] += 1
        self.context.note(intent=intent)

        if intent in ("what_is", "find", "ocr", "zoom", "translate"):
            return self._do_vision(routed, text)
        if intent == "set_ui_mode":
            return self._do_device({"type": "device_command", "action": "set_ui_mode", "ui_mode": routed["ui_mode"]}, intent)
        if intent == "privacy_pause":
            return self._do_device({"type": "device_command", "action": "privacy_pause"}, intent)
        if intent == "open_app":
            return self._do_open_app(routed)
        if intent == "menu":
            return self._do_device({"type": "device_command", "action": "open_menu"}, intent)
        if intent == "paid_mode":
            return self._do_paid_mode(routed)
        if intent == "local_mode":
            return self._do_local_mode()
        if intent == "ask_memory":
            return self._do_ask_memory(routed)
        if intent == "replay":
            return self._do_replay(routed)
        if intent == "set_tts":
            on = routed.get("tts")
            return self._do_device({"type": "device_command", "action": "set_tts", "tts": bool(on)}, intent)
        if intent == "owner_enroll":
            return self._do_owner_enroll()
        return self._unknown(text)

    def _do_owner_enroll(self) -> RoutedIntent:
        """E37 §3: arm the wearer's voice-enrolment capture window."""
        if self.owner_setup is None:
            return self._unavailable("owner_enroll", "Configuration de la voix indisponible sur ce profil.")
        try:
            res = self.owner_setup.begin()
        except Exception:
            return self._unavailable("owner_enroll", "Impossible de démarrer la configuration de ta voix.")
        return RoutedIntent(intent="owner_enroll", result=res, handled=True)

    def _do_vision(self, routed: dict[str, Any], text: str) -> RoutedIntent:
        intent = routed["intent"]
        request: dict[str, Any] = {
            "kind": "what_is" if intent in ("zoom", "translate", "what_is") else intent,
            "track_id": routed.get("track_id") or self.context.last_track_id,
            "bbox": routed.get("bbox") or self.context.last_bbox,
        }
        if intent == "find":
            request["kind"] = "find"
            request["query"] = routed.get("query")
        if intent == "translate":
            request["translate"] = True
            request["language"] = routed.get("language", "fr")
        if intent == "zoom":
            request["zoom"] = True
        result = None
        if self.vision_focus is not None:
            try:
                result = self.vision_focus(request)
            except Exception:
                result = None
        # Note the target so a follow-up deixis resolves on it.
        self.context.note(intent=intent, track_id=request.get("track_id"), bbox=request.get("bbox"))
        return RoutedIntent(intent=intent, request=request, result=result, handled=self.vision_focus is not None)

    def _do_device(self, cmd: dict[str, Any], intent: str) -> RoutedIntent:
        handled = False
        if self.on_device_command is not None:
            try:
                self.on_device_command(cmd)
                handled = True
            except Exception:
                handled = False
        return RoutedIntent(intent=intent, device_command=cmd, handled=handled)

    def _do_open_app(self, routed: dict[str, Any]) -> RoutedIntent:
        cmd: dict[str, Any] = {"type": "device_command", "action": "open_app", "app": routed.get("app")}
        for k in ("destination", "query", "package"):
            if routed.get(k):
                cmd[k] = routed[k]
        return self._do_device(cmd, "open_app")

    def _do_replay(self, routed: dict[str, Any]) -> RoutedIntent:
        time = routed.get("time")
        # E35: a real ReplayService assembles the bundle + emits virtual_screen +
        # timeline. Its ``emit_ui_intent`` is the same DataChannel push as ours.
        if self.replay_service is not None:
            try:
                res = self.replay_service.replay(time=time)
            except Exception:
                res = None
            if res is not None:
                self.context.note(intent="replay")
                return RoutedIntent(intent="replay", replay=res, handled=True,
                                    device_command={"type": "device_command", "action": "replay", "time": time})
        # No service wired → the phone's own replay UI via the device_command path.
        return self._do_device({"type": "device_command", "action": "replay", "time": time}, "replay")

    def _do_paid_mode(self, routed: dict[str, Any]) -> RoutedIntent:
        if self.llm_router is None:
            return self._unavailable("paid_mode", "Mode payant indisponible (pas de routeur LLM).")
        res = self.llm_router.switch_to_cloud(routed.get("provider", "openai"))
        self._ui(self._toast(res.get("text", ""), level="confirm" if res.get("ok") else "warn"))
        return RoutedIntent(intent="paid_mode", result=res, handled=True)

    def _do_local_mode(self) -> RoutedIntent:
        if self.llm_router is None:
            return self._unavailable("local_mode", "Déjà en local.")
        res = self.llm_router.switch_to_local()
        self._ui(self._toast(res.get("text", ""), level="confirm"))
        return RoutedIntent(intent="local_mode", result=res, handled=True)

    def _do_ask_memory(self, routed: dict[str, Any]) -> RoutedIntent:
        question = routed.get("question") or ""
        if self.ask_memory is None:
            return self._unavailable("ask_memory", "Mémoire indisponible.")
        try:
            intent = self.ask_memory(question)
        except Exception:
            return self._unavailable("ask_memory", "Erreur mémoire.")
        self._ui(intent)
        self.context.note(intent="ask_memory", text=str(intent.get("content", {}).get("text")))
        return RoutedIntent(intent="ask_memory", ui_intent=intent, handled=True)

    def _toast(self, text: str, *, level: str = "confirm") -> dict[str, Any]:
        return {
            "type": "ui_intent", "ui_intent_id": str(uuid.uuid4()),
            "producer": "ultralive", "component": "context_card",
            "content": {"kind": "toast", "text": text, "level": level},
            "truth_level": "observed", "confidence": 1.0, "priority": 0.5, "ttl_ms": 6000,
            "evidence_refs": [],
        }

    def _unavailable(self, intent: str, text: str) -> RoutedIntent:
        self._ui(self._toast(text, level="warn"))
        return RoutedIntent(intent=intent, handled=False, text=text)
