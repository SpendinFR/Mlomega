from __future__ import annotations

import json
from collections import defaultdict

from .db import connect, upsert
from .graph_memory import ensure_entity, add_relation
from .memory_foundation import (
    TRUTH_CONSOLIDATED,
    add_memory_card,
    add_memory_facet,
    add_memory_link,
)
from .utils import json_dumps, now_iso, stable_id, normalize_text


def _stance_class(stance: str | None, content: str) -> str:
    """Use the stance already produced by the local LLM.

    No keyword remapping is performed here: consolidation groups the semantic
    labels emitted by the microscope instead of applying another rule layer.
    """
    return (stance or "unclassified").strip() or "unclassified"


def consolidate_reflections() -> list[str]:
    """Build/update reflection states per person+topic from atomic memories.

    This is the piece that lets the system notice: same topic + same person/other person + different time → stable, changed, loop, contradiction.
    """
    created: list[str] = []
    with connect() as con:
        memories = list(con.execute(
            """SELECT memory_id, kind, person_id, topic, content, stance, memory_time, confidence, source_conversation_id
               FROM atomic_memories
               WHERE topic IS NOT NULL
               ORDER BY COALESCE(memory_time, created_at), created_at"""
        ))
        grouped: dict[tuple[str, str], list] = defaultdict(list)
        for m in memories:
            grouped[(m["person_id"] or "unknown", m["topic"] or "unknown")].append(m)
        for (person_id, topic), rows in grouped.items():
            if not rows:
                continue
            # split by LLM stance label so shifts can be represented.
            chunks: list[list] = []
            current: list = []
            last_class = None
            for m in rows:
                cls = _stance_class(m["stance"], m["content"])
                if current and cls != last_class:
                    chunks.append(current)
                    current = []
                current.append(m)
                last_class = cls
            if current:
                chunks.append(current)

            prev_state_id = None
            prev_state_card_id = None
            for chunk in chunks:
                first = chunk[0]
                last = chunk[-1]
                cls = _stance_class(last["stance"], last["content"])
                topic_eid = ensure_entity(con, "topic", topic)
                summary = _summarize_state(person_id, topic, cls, chunk)
                state_id = stable_id("state", person_id, topic, cls, first["memory_id"], last["memory_id"])
                upsert(con, "reflection_states", {
                    "state_id": state_id,
                    "subject_entity_id": topic_eid,
                    "person_id": person_id,
                    "topic": topic,
                    "stance": cls,
                    "summary": summary,
                    "period_start": first["memory_time"],
                    "period_end": last["memory_time"],
                    "evidence_count": len(chunk),
                    "confidence": min(0.95, 0.55 + len(chunk) * 0.08),
                    "created_at": now_iso(),
                }, "state_id")
                created.append(state_id)
                state_card_id = add_memory_card(
                    con,
                    source_table="reflection_states",
                    source_id=state_id,
                    card_type="reflection_state",
                    truth_status=TRUTH_CONSOLIDATED,
                    title=f"État de réflexion: {person_id} / {topic} / {cls}",
                    summary=summary,
                    person_id=person_id,
                    topic=topic,
                    time_start=first["memory_time"],
                    time_end=last["memory_time"],
                    confidence=min(0.95, 0.55 + len(chunk) * 0.08),
                    evidence_count=len(chunk),
                    metadata={"source_memory_ids": [r["memory_id"] for r in chunk], "stance": cls},
                )
                add_memory_facet(con, target_table="memory_cards", target_id=state_card_id, facet_type="stance", facet_value=cls, source="consolidation", confidence=0.9)
                for source_memory in chunk:
                    src_card = con.execute("SELECT card_id FROM memory_cards WHERE source_table='atomic_memories' AND source_id=?", (source_memory["memory_id"],)).fetchone()
                    if src_card:
                        add_memory_link(con, from_table="memory_cards", from_id=state_card_id, relation_type="consolidates", to_table="memory_cards", to_id=src_card["card_id"], confidence=source_memory["confidence"] or 0.7, evidence_text=source_memory["content"])
                peid = ensure_entity(con, "person", person_id)
                add_relation(con, peid, "has_reflection_state_on", topic_eid, valid_from=first["memory_time"], confidence=0.75, evidence_type="reflection_state", evidence_id=state_id, context={"stance": cls, "summary": summary})
                if prev_state_card_id:
                    add_memory_link(con, from_table="memory_cards", from_id=prev_state_card_id, relation_type="next_reflection_state", to_table="memory_cards", to_id=state_card_id, confidence=0.82, evidence_text=summary)
                if prev_state_id:
                    prev = con.execute("SELECT stance, summary FROM reflection_states WHERE state_id=?", (prev_state_id,)).fetchone()
                    edge_type = "stable_loop" if prev and prev["stance"] == cls else "stance_shift"
                    explanation = "La réflexion reste dans la même famille de position." if edge_type == "stable_loop" else f"La réflexion passe de '{prev['stance'] if prev else 'unknown'}' à '{cls}'."
                    edge_id = stable_id("redge", prev_state_id, state_id, edge_type)
                    upsert(con, "reflection_edges", {
                        "edge_id": edge_id,
                        "from_state_id": prev_state_id,
                        "to_state_id": state_id,
                        "edge_type": edge_type,
                        "explanation": explanation,
                        "confidence": 0.78,
                        "created_at": now_iso(),
                    }, "edge_id")
                prev_state_id = state_id
                prev_state_card_id = state_card_id
        con.commit()
    return created


def _summarize_state(person_id: str, topic: str, stance: str, rows: list) -> str:
    examples = [r["content"][:160] for r in rows[-3:]]
    return f"Sur '{topic}', {person_id} est dans une phase '{stance}'. Indices récents : " + " | ".join(examples)


def consolidate_patterns() -> list[str]:
    created: list[str] = []
    with connect() as con:
        # expression frequency patterns
        for row in con.execute(
            """SELECT normalized, expression, category, COUNT(*) AS c, MIN(created_at) AS first_seen, MAX(created_at) AS last_seen
               FROM expression_signals GROUP BY normalized, expression, category HAVING c >= 1"""
        ):
            if row["c"] < 1:
                continue
            pid = stable_id("pattern", "expression", row["normalized"])
            desc = f"Expression récurrente ou significative : '{row['expression']}' ({row['category']}). Elle sert de signal personnel à interpréter selon le contexte."
            upsert(con, "patterns", {
                "pattern_id": pid,
                "pattern_type": "personal_expression",
                "scope": "conversation_language",
                "title": f"Expression personnelle : {row['expression']}",
                "description": desc,
                "evidence_count": row["c"],
                "confidence": min(0.95, 0.55 + row["c"] * 0.1),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "metadata_json": json_dumps({"category": row["category"]}),
                "created_at": now_iso(),
            }, "pattern_id")
            pattern_card_id = add_memory_card(con, source_table="patterns", source_id=pid, card_type="pattern:personal_expression", truth_status=TRUTH_CONSOLIDATED, title=f"Expression personnelle : {row['expression']}", summary=desc, topic="conversation_language", confidence=min(0.95, 0.55 + row["c"] * 0.1), evidence_count=row["c"], time_start=row["first_seen"], time_end=row["last_seen"], metadata={"category": row["category"]})
            add_memory_facet(con, target_table="memory_cards", target_id=pattern_card_id, facet_type="pattern_type", facet_value="personal_expression", source="consolidation", confidence=1.0)
            created.append(pid)
        # person/relationship activation patterns: how the user or a person tends to react
        # when a specific trigger/emotion appears with a given person/topic.
        for row in con.execute(
            """SELECT person_id, COALESCE(other_person_id, '') AS other_person_id, COALESCE(topic, 'conversation') AS topic,
                      COALESCE(trigger_summary, '') AS trigger_summary, COALESCE(emotion, '') AS emotion,
                      COALESCE(reaction_rule, '') AS reaction_rule,
                      COUNT(*) AS c, MIN(created_at) AS first_seen, MAX(created_at) AS last_seen,
                      AVG(confidence) AS avg_conf
               FROM activation_signals
               WHERE trigger_summary IS NOT NULL AND trigger_summary != ''
               GROUP BY person_id, other_person_id, topic, trigger_summary, emotion, reaction_rule
               HAVING c >= 1"""
        ):
            trigger_norm = normalize_text(row["trigger_summary"])
            pid = stable_id("reaction_pattern", row["person_id"], row["other_person_id"], row["topic"], trigger_norm, row["emotion"], row["reaction_rule"])
            desc = f"Quand {row['person_id'] or 'unknown'} rencontre le déclencheur '{row['trigger_summary']}' sur '{row['topic']}', émotion détectée: {row['emotion']}; réaction/règle probable: {row['reaction_rule']}"
            upsert(con, "person_reaction_patterns", {
                "pattern_id": pid,
                "person_id": row["person_id"],
                "other_person_id": row["other_person_id"] or None,
                "topic": row["topic"],
                "trigger_norm": trigger_norm,
                "emotion": row["emotion"],
                "typical_reaction": row["reaction_rule"],
                "evidence_count": row["c"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "confidence": min(0.95, float(row["avg_conf"] or 0.7) + min(row["c"], 5) * 0.03),
                "metadata_json": json_dumps({"trigger_summary": row["trigger_summary"], "emotion": row["emotion"]}),
                "created_at": now_iso(),
            }, "pattern_id")
            upsert(con, "patterns", {
                "pattern_id": stable_id("pattern", "person_reaction", pid),
                "pattern_type": "person_reaction",
                "scope": "person_topic_relationship",
                "title": f"Réaction typique: {row['person_id'] or 'unknown'} / {row['topic']}",
                "description": desc,
                "evidence_count": row["c"],
                "confidence": min(0.95, float(row["avg_conf"] or 0.7) + min(row["c"], 5) * 0.03),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "metadata_json": json_dumps({"source_pattern_id": pid}),
                "created_at": now_iso(),
            }, "pattern_id")
            pattern_card_id = add_memory_card(con, source_table="person_reaction_patterns", source_id=pid, card_type="pattern:person_reaction", truth_status=TRUTH_CONSOLIDATED, title=f"Réaction typique: {row['person_id'] or 'unknown'}", summary=desc, person_id=row["person_id"], topic=row["topic"], confidence=min(0.95, float(row["avg_conf"] or 0.7) + min(row["c"], 5) * 0.03), evidence_count=row["c"], time_start=row["first_seen"], time_end=row["last_seen"], metadata={"other_person_id": row["other_person_id"] or None, "trigger_summary": row["trigger_summary"], "emotion": row["emotion"]})
            add_memory_facet(con, target_table="memory_cards", target_id=pattern_card_id, facet_type="pattern_type", facet_value="person_reaction", source="consolidation", confidence=1.0)
            add_memory_facet(con, target_table="memory_cards", target_id=pattern_card_id, facet_type="activation_trigger", facet_value=row["trigger_summary"], source="consolidation", confidence=0.85)
            add_memory_facet(con, target_table="memory_cards", target_id=pattern_card_id, facet_type="emotion", facet_value=row["emotion"], source="consolidation", confidence=0.85)
            created.append(pid)

        # stance shifts and loops
        for row in con.execute(
            """SELECT edge_type, COUNT(*) AS c FROM reflection_edges GROUP BY edge_type"""
        ):
            pid = stable_id("pattern", "reflection_edge", row["edge_type"])
            title = "Boucle de réflexion" if row["edge_type"] == "stable_loop" else "Changement de réflexion"
            desc = "Le système observe que certaines positions reviennent ou restent stables dans le temps." if row["edge_type"] == "stable_loop" else "Le système observe au moins un passage d'une famille de position à une autre."
            upsert(con, "patterns", {
                "pattern_id": pid,
                "pattern_type": row["edge_type"],
                "scope": "reflection_timeline",
                "title": title,
                "description": desc,
                "evidence_count": row["c"],
                "confidence": min(0.9, 0.6 + row["c"] * 0.08),
                "first_seen": None,
                "last_seen": None,
                "metadata_json": "{}",
                "created_at": now_iso(),
            }, "pattern_id")
            pattern_card_id = add_memory_card(con, source_table="patterns", source_id=pid, card_type=f"pattern:{row['edge_type']}", truth_status=TRUTH_CONSOLIDATED, title=title, summary=desc, topic="reflection_timeline", confidence=min(0.9, 0.6 + row["c"] * 0.08), evidence_count=row["c"], metadata={"edge_type": row["edge_type"]})
            add_memory_facet(con, target_table="memory_cards", target_id=pattern_card_id, facet_type="pattern_type", facet_value=row["edge_type"], source="consolidation", confidence=1.0)
            created.append(pid)
        con.commit()
    return created


def consolidate_self_model() -> list[str]:
    facts: list[str] = []
    with connect() as con:
        # From frequent expressions and emotions, derive stable user model facts.
        exprs = list(con.execute("SELECT expression, category, COUNT(*) AS c FROM expression_signals GROUP BY expression, category ORDER BY c DESC LIMIT 12"))
        for e in exprs:
            content = f"Quand l'utilisateur emploie '{e['expression']}', c'est un signal de type {e['category']} qui doit influencer le style de réponse."
            fid = stable_id("self", "expr", e["expression"], e["category"])
            upsert(con, "self_model_facts", {
                "fact_id": fid,
                "fact_type": "personal_language",
                "content": content,
                "scope": "conversation",
                "evidence_count": e["c"],
                "confidence": min(0.95, 0.6 + e["c"] * 0.08),
                "valid_from": None,
                "valid_until": None,
                "metadata_json": json_dumps({"expression": e["expression"], "category": e["category"]}),
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }, "fact_id")
            fact_card_id = add_memory_card(con, source_table="self_model_facts", source_id=fid, card_type="self_model:personal_language", truth_status=TRUTH_CONSOLIDATED, title=f"Modèle utilisateur: {e['expression']}", summary=content, topic="self_model", confidence=min(0.95, 0.6 + e["c"] * 0.08), evidence_count=e["c"], metadata={"expression": e["expression"], "category": e["category"]})
            add_memory_facet(con, target_table="memory_cards", target_id=fact_card_id, facet_type="self_model_fact", facet_value="personal_language", source="consolidation", confidence=1.0)
            facts.append(fid)
        # From utterance analyses.
        rows = list(con.execute("SELECT hidden_expectation, COUNT(*) AS c FROM utterance_analyses GROUP BY hidden_expectation ORDER BY c DESC LIMIT 8"))
        for r in rows:
            fid = stable_id("self", "expectation", r["hidden_expectation"])
            upsert(con, "self_model_facts", {
                "fact_id": fid,
                "fact_type": "hidden_expectation",
                "content": f"Attente récurrente détectée : {r['hidden_expectation']}",
                "scope": "conversation",
                "evidence_count": r["c"],
                "confidence": min(0.92, 0.55 + r["c"] * 0.08),
                "valid_from": None,
                "valid_until": None,
                "metadata_json": "{}",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }, "fact_id")
            fact_card_id = add_memory_card(con, source_table="self_model_facts", source_id=fid, card_type="self_model:hidden_expectation", truth_status=TRUTH_CONSOLIDATED, title="Modèle utilisateur: attente cachée", summary=f"Attente récurrente détectée : {r['hidden_expectation']}", topic="self_model", confidence=min(0.92, 0.55 + r["c"] * 0.08), evidence_count=r["c"], metadata={"hidden_expectation": r["hidden_expectation"]})
            add_memory_facet(con, target_table="memory_cards", target_id=fact_card_id, facet_type="self_model_fact", facet_value="hidden_expectation", source="consolidation", confidence=1.0)
            facts.append(fid)
        con.commit()
    return facts


def consolidate_all() -> dict:
    with connect() as con:
        run_id = stable_id("run", "consolidate", now_iso())
        upsert(con, "consolidation_runs", {"run_id": run_id, "run_type": "full", "started_at": now_iso(), "finished_at": None, "summary": None, "metadata_json": "{}"}, "run_id")
        con.commit()
    states = consolidate_reflections()
    patterns = consolidate_patterns()
    facts = consolidate_self_model()
    with connect() as con:
        con.execute("UPDATE consolidation_runs SET finished_at=?, summary=?, metadata_json=? WHERE run_id=?", (now_iso(), f"states={len(states)} patterns={len(patterns)} facts={len(facts)}", json_dumps({"states": states, "patterns": patterns, "facts": facts}), run_id))
        con.commit()
    return {"run_id": run_id, "states": len(states), "patterns": len(patterns), "facts": len(facts)}
