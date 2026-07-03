from __future__ import annotations

"""Utterance-level segmentation for elite memory ingestion.

WhisperX returns time-aligned segments, but a segment is not always the same thing
as a human sentence / meaningful utterance. The memory layer needs smaller,
source-grounded units so the microscope analyzes one intention-bearing unit at a
time while still seeing the surrounding context.
"""

import re
from copy import deepcopy
from typing import Any

from .utils import stable_id

END_PUNCT_RE = re.compile(r"[.!?…]+[\"'»”’)]*$")
SENTENCE_RE = re.compile(r"[^.!?…]+(?:[.!?…]+|$)", re.UNICODE)


def _word_text(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or word.get("token") or "").strip()


def _word_start(word: dict[str, Any]) -> float | None:
    value = word.get("start")
    try:
        return None if value is None else float(value)
    except Exception:
        return None


def _word_end(word: dict[str, Any]) -> float | None:
    value = word.get("end")
    try:
        return None if value is None else float(value)
    except Exception:
        return None


def _join_words(words: list[dict[str, Any]]) -> str:
    text = " ".join(_word_text(w) for w in words if _word_text(w)).strip()
    # Keep punctuation natural after simple whitespace reconstruction.
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = re.sub(r"([\(\[«])\s+", r"\1", text)
    text = re.sub(r"\s+([\)\]»])", r"\1", text)
    return text


def _split_words(
    words: list[dict[str, Any]],
    *,
    max_words: int,
    max_duration_s: float,
    pause_split_s: float,
) -> list[tuple[str, float | None, float | None, list[dict[str, Any]]]]:
    clean_words = [w for w in words if _word_text(w)]
    if not clean_words:
        return []

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    for w in clean_words:
        should_split_before = False
        if current:
            previous_end = _word_end(current[-1])
            current_start = _word_start(w)
            chunk_start = _word_start(current[0])
            current_end = _word_end(w)
            if previous_end is not None and current_start is not None and current_start - previous_end >= pause_split_s:
                should_split_before = True
            if chunk_start is not None and current_end is not None and current_end - chunk_start >= max_duration_s:
                should_split_before = True
            if len(current) >= max_words:
                should_split_before = True

        if should_split_before:
            chunks.append(current)
            current = []

        current.append(w)

        token = _word_text(w)
        if END_PUNCT_RE.search(token) and len(current) >= 2:
            chunks.append(current)
            current = []

    if current:
        chunks.append(current)

    out: list[tuple[str, float | None, float | None, list[dict[str, Any]]]] = []
    for chunk in chunks:
        text = _join_words(chunk)
        if not text:
            continue
        start = next((_word_start(w) for w in chunk if _word_start(w) is not None), None)
        end = next((_word_end(w) for w in reversed(chunk) if _word_end(w) is not None), None)
        out.append((text, start, end, chunk))
    return out


def _split_text(text: str, *, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    sentences = [m.group(0).strip() for m in SENTENCE_RE.finditer(text) if m.group(0).strip()]
    if not sentences:
        sentences = [text]
    out: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            out.append(sentence)
            continue
        parts = re.split(r"(?<=[,;:])\s+", sentence)
        current = ""
        for part in parts:
            candidate = (current + " " + part).strip() if current else part.strip()
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    out.append(current)
                current = part.strip()
        if current:
            out.append(current)
    return out


def normalize_transcript_turns(
    data: dict[str, Any],
    *,
    max_words: int = 32,
    max_duration_s: float = 14.0,
    pause_split_s: float = 0.75,
    max_chars_without_words: int = 260,
) -> dict[str, Any]:
    """Return a transcript whose turns are atomic utterances.

    The original transcript is preserved in metadata under ``original_turn`` for
    every generated utterance. If a source already looks atomic, it is kept but
    still marked with the segmentation metadata.
    """
    normalized = deepcopy(data)
    meta = dict(normalized.get("metadata", {}))
    conversation_id = meta.get("conversation_id") or "conversation"
    new_turns: list[dict[str, Any]] = []

    for original_idx, turn in enumerate(normalized.get("turns", [])):
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        original_turn_id = turn.get("turn_id") or stable_id("rawturn", conversation_id, original_idx, turn.get("speaker"), text)
        words = turn.get("words") if isinstance(turn.get("words"), list) else []
        split_units = _split_words(words, max_words=max_words, max_duration_s=max_duration_s, pause_split_s=pause_split_s)

        if not split_units:
            texts = _split_text(text, max_chars=max_chars_without_words)
            split_units = []
            for i, sentence in enumerate(texts):
                # Without word timestamps, keep the original time range on each
                # child and rely on char offsets/evidence text for grounding.
                split_units.append((sentence, turn.get("start"), turn.get("end"), []))

        for local_idx, (unit_text, start_s, end_s, unit_words) in enumerate(split_units):
            if not unit_text:
                continue
            child = dict(turn)
            child["turn_id"] = stable_id("utt", conversation_id, original_turn_id, local_idx, unit_text, start_s, end_s)
            child["text"] = unit_text
            child["start"] = start_s
            child["end"] = end_s
            child["words"] = unit_words
            child["metadata"] = {
                **(turn.get("metadata") or {}),
                "segmentation_level": "atomic_utterance",
                "segmentation_version": "3.2.2-precise-context",
                "original_turn_id": original_turn_id,
                "original_turn_index": original_idx,
                "utterance_index_in_original_turn": local_idx,
                "original_text": text,
                "rules": {
                    "max_words": max_words,
                    "max_duration_s": max_duration_s,
                    "pause_split_s": pause_split_s,
                    "max_chars_without_words": max_chars_without_words,
                },
            }
            new_turns.append(child)

    meta["segmentation"] = {
        "level": "atomic_utterance",
        "version": "3.2.2-precise-context",
        "max_words": max_words,
        "max_duration_s": max_duration_s,
        "pause_split_s": pause_split_s,
        "max_chars_without_words": max_chars_without_words,
        "source_turn_count": len(normalized.get("turns", [])),
        "utterance_count": len(new_turns),
    }
    normalized["metadata"] = meta
    normalized["turns"] = new_turns
    return normalized


def build_context_window(turns: list[dict[str, Any]], idx: int, *, before: int = 3, after: int = 2) -> dict[str, Any]:
    """Small local context passed to the LLM while analyzing one utterance."""
    def pack(i: int, turn: dict[str, Any]) -> dict[str, Any]:
        return {
            "relative_index": i - idx,
            "speaker": turn.get("person_id") or turn.get("speaker") or turn.get("speaker_label"),
            "start": turn.get("start"),
            "end": turn.get("end"),
            "text": turn.get("text"),
        }

    return {
        "before": [pack(i, turns[i]) for i in range(max(0, idx - before), idx)],
        "current": pack(idx, turns[idx]),
        "after": [pack(i, turns[i]) for i in range(idx + 1, min(len(turns), idx + 1 + after))],
        "instruction": "Analyser uniquement current; utiliser before/after seulement pour désambiguïser le contexte, le sujet, l'émotion et l'intention.",
    }
