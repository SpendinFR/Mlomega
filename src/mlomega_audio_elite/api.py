from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    from fastapi import FastAPI, UploadFile, File, Form, Body
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover
    FastAPI = None  # type: ignore

from .db import init_db
from .ingest import ingest_transcript_file, ingest_audio
from .retrieval import answer, search
from .consolidation import consolidate_all
from .voice_identity import enroll_voice, match_voice
from .v19_visual_store import store_scene_summary, store_ui_outcome, store_visual_event

if FastAPI is not None:
    app = FastAPI(title="MemoryLight Omega Audio Elite V3.1 Strict")

    @app.on_event("startup")
    def _startup():
        init_db()

    @app.get("/health")
    def health():
        return {"ok": True, "system": "MemoryLight Omega Audio Elite V3.1 Strict"}

    @app.post("/ingest/transcript")
    async def upload_transcript(file: UploadFile = File(...)):
        suffix = Path(file.filename or "transcript.json").suffix or ".json"
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        conv_id = ingest_transcript_file(tmp_path)
        return {"conversation_id": conv_id}

    @app.post("/ingest/audio")
    async def upload_audio(file: UploadFile = File(...), language: str = Form("fr")):
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        conv_id = ingest_audio(tmp_path, language=language)
        return {"conversation_id": conv_id}

    @app.post("/voice/enroll")
    async def enroll(person_id: str = Form(...), is_user: bool = Form(False), file: UploadFile = File(...)):
        suffix = Path(file.filename or "voice.wav").suffix or ".wav"
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        embedding_id = enroll_voice(person_id, tmp_path, is_user=is_user)
        return {"embedding_id": embedding_id}

    @app.post("/voice/match")
    async def match(file: UploadFile = File(...)):
        suffix = Path(file.filename or "voice.wav").suffix or ".wav"
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        return match_voice(tmp_path)

    @app.get("/query")
    def query(q: str):
        return {"answer": answer(q), "hits": [h.__dict__ for h in search(q)]}

    @app.post("/consolidate")
    def consolidate():
        return consolidate_all()


    # V19 XR/live visual memory endpoints.  Payloads must carry explicit
    # memory_owner_id; the store rejects implicit owner inference.
    @app.post("/ingest/visual-event")
    def ingest_visual_event(payload: dict = Body(...)):
        return {"visual_event_id": store_visual_event(payload)}

    @app.post("/ingest/scene-summary")
    def ingest_scene_summary(payload: dict = Body(...)):
        return {"scene_summary_id": store_scene_summary(payload)}

    @app.post("/memory/correction-visual")
    def correction_visual(payload: dict = Body(...)):
        event_id = store_visual_event({**payload, "event_type": payload.get("event_type") or "visual_correction", "truth_level": payload.get("truth_level") or "observed"})
        return {"visual_event_id": event_id, "status": "recorded"}

    @app.get("/xr/session-health")
    def xr_session_health(memory_owner_id: str, live_session_id: str):
        return {"ok": True, "memory_owner_id": memory_owner_id, "live_session_id": live_session_id, "system": "MLOmega V19 XR"}

    @app.post("/evidence/request-clip")
    def evidence_request_clip(payload: dict = Body(...)):
        outcome_id = store_ui_outcome({**payload, "event": payload.get("event") or "clip_requested", "source": payload.get("source") or "evidence_request"})
        return {"ui_outcome_id": outcome_id, "status": "queued"}
else:  # pragma: no cover
    app = None
