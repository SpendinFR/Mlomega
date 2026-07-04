from __future__ import annotations

"""FaceIdentity — local facial detection + embedding + gallery (E32 §1).

The identity layer's *visual* cue. On a person crop coming from VisionRT it runs:

    crop -> YuNet (cv2.FaceDetectorYN) detect -> largest face -> alignCrop
         -> SFace (cv2.FaceRecognizerSF) 128-D L2 embedding
         -> cosine match against the local face gallery

Both weights are OpenCV-Zoo ONNX models pinned in ``configs/MODEL_MANIFEST.yaml``
(YuNet MIT, SFace Apache-2.0), fetched by ``scripts/fetch_models_v19.py``. They
run through OpenCV's own ``cv2.FaceDetectorYN`` / ``cv2.FaceRecognizerSF`` — no
extra runtime dependency beyond ``opencv-python`` (already required for VisionRT).

The gallery is a **service-local SQLite file** (``face_identity_v19``), living
*next to* the core just like WorldBrain's session store — never a new core table
(piège #11). It holds one row per stored embedding:
``person_id, name, embedding(json), source, created_at`` plus a ``face_people``
name table.

Thresholds are config (``MLOMEGA_FACE_*`` env / SceneAdapter passthrough), never
hardcoded literals in the matching path. Matching returns the best person above
the cosine threshold, or ``None`` (anonymous) below it — §17.2: never a name under
confidence.

The embedder is injectable (``embedder=``) so tests and the fusion layer can drive
the exact same matching logic with a substitute when the ONNX weights are absent.
"""

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _l2_normalize(vec: Sequence[float]) -> list[float]:
    s = sum(float(v) * float(v) for v in vec) ** 0.5 or 1.0
    return [float(v) / s for v in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = sum(float(x) * float(x) for x in a) ** 0.5 or 1.0
    nb = sum(float(y) * float(y) for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


class FaceIdentityError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config
@dataclass
class FaceConfig:
    """Face detection / matching thresholds (all config, never hardcoded)."""

    detector_path: str = ""
    embedder_path: str = ""
    detect_score_threshold: float = 0.6   # YuNet confidence floor for a face
    nms_threshold: float = 0.3
    match_threshold: float = 0.363        # SFace cosine floor (OpenCV Zoo default)
    min_face_px: int = 24                 # ignore tiny faces (unreliable embeddings)

    @classmethod
    def from_env(cls, profile: dict[str, Any] | None = None) -> "FaceConfig":
        p = (profile or {}).get("face", {}) if isinstance(profile, dict) else {}
        det = str(p.get("detector_path") or os.environ.get(
            "MLOMEGA_FACE_DETECTOR", str(_ROOT / "models" / "face_detection_yunet_2023mar.onnx")))
        emb = str(p.get("embedder_path") or os.environ.get(
            "MLOMEGA_FACE_EMBEDDER", str(_ROOT / "models" / "face_recognition_sface_2021dec.onnx")))
        return cls(
            detector_path=det,
            embedder_path=emb,
            detect_score_threshold=float(p.get("detect_score_threshold", os.environ.get("MLOMEGA_FACE_DETECT_SCORE", 0.6))),
            match_threshold=float(p.get("match_threshold", os.environ.get("MLOMEGA_FACE_THRESHOLD", 0.363))),
            min_face_px=int(p.get("min_face_px", os.environ.get("MLOMEGA_FACE_MIN_PX", 24))),
        )


# --------------------------------------------------------------------------- embedder
class SFaceEmbedder:
    """YuNet detect + SFace embed via OpenCV. Detects, aligns, returns a 128-D vec.

    ``embed_bgr`` returns ``(embedding, face_box)`` for the largest face, or
    ``(None, None)`` when no face passes the detection threshold. Kept minimal so
    the gallery/matching logic (below) is model-agnostic and testable.
    """

    model = "opencv-yunet-sface"

    def __init__(self, config: FaceConfig, *, arbiter: Any = None) -> None:
        self.config = config
        self.arbiter = arbiter
        import cv2  # opencv-python is a hard VisionRT dep already

        if not Path(config.detector_path).exists():
            raise FaceIdentityError(f"YuNet weights absent: {config.detector_path} (run scripts/fetch_models_v19.py)")
        if not Path(config.embedder_path).exists():
            raise FaceIdentityError(f"SFace weights absent: {config.embedder_path} (run scripts/fetch_models_v19.py)")
        self._cv2 = cv2
        self._detector = cv2.FaceDetectorYN.create(
            config.detector_path, "", (320, 320),
            float(config.detect_score_threshold), float(config.nms_threshold), 5000,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(config.embedder_path, "")

    def embed_bgr(self, crop_bgr: Any) -> tuple[list[float] | None, list[int] | None]:
        import numpy as np

        if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
            return None, None
        h, w = crop_bgr.shape[:2]
        if h < self.config.min_face_px or w < self.config.min_face_px:
            return None, None
        # GpuArbiter "ocr"-class admission when present (SFace is tiny but honour
        # the arbiter so a busy GPU is never contended — CPU fallback is fine).
        if self.arbiter is not None and hasattr(self.arbiter, "try_acquire"):
            try:
                token = self.arbiter.try_acquire("ocr")
            except Exception:
                token = None
        else:
            token = None
        try:
            self._detector.setInputSize((w, h))
            n, faces = self._detector.detect(crop_bgr)
            if faces is None or len(faces) == 0:
                return None, None
            # Largest face (biggest area) — the person the crop is centred on.
            faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
            face = faces[0]
            fw, fh = int(face[2]), int(face[3])
            if fw < self.config.min_face_px or fh < self.config.min_face_px:
                return None, None
            aligned = self._recognizer.alignCrop(crop_bgr, face)
            feat = self._recognizer.feature(aligned)
            vec = _l2_normalize(np.asarray(feat, dtype="float32").reshape(-1).tolist())
            box = [int(face[0]), int(face[1]), fw, fh]
            return vec, box
        finally:
            if token is not None and hasattr(token, "release"):
                try:
                    token.release()
                except Exception:
                    pass


# --------------------------------------------------------------------------- gallery
class FaceIdentity:
    """Face gallery + matching. Model-agnostic via an injectable ``embedder``.

    ``enroll(person_id, name, crop_bgr)`` stores an embedding; ``match(crop_bgr)``
    returns ``{person_id, name, score, matched, candidates}``. Below the cosine
    threshold ``matched`` is False and ``name`` is None (§17.2).
    """

    def __init__(
        self,
        *,
        config: FaceConfig | None = None,
        embedder: Any = None,
        service_db_path: str | Path | None = None,
        arbiter: Any = None,
    ) -> None:
        self.config = config or FaceConfig.from_env()
        self._embedder = embedder  # lazy; real SFace built on first use if None
        self.arbiter = arbiter
        self._db = self._init_db(service_db_path)
        self.metrics = {"enrollments": 0, "matches": 0, "no_face": 0}

    # ---------------------------------------------------------------- embedder
    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            self._embedder = SFaceEmbedder(self.config, arbiter=self.arbiter)
        return self._embedder

    @property
    def available(self) -> bool:
        try:
            return self.embedder is not None
        except Exception:
            return False

    # ---------------------------------------------------------------- store
    def _init_db(self, path: str | Path | None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path) if path else ":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS face_people(
                 person_id TEXT PRIMARY KEY, name TEXT,
                 created_at TEXT, updated_at TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS face_embeddings(
                 embedding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                 person_id TEXT, name TEXT, embedding TEXT,
                 source TEXT, created_at TEXT)"""
        )
        conn.commit()
        return conn

    def _upsert_person(self, person_id: str, name: str) -> None:
        now = _now_iso()
        self._db.execute(
            """INSERT INTO face_people(person_id, name, created_at, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(person_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
            (person_id, name, now, now),
        )

    # ---------------------------------------------------------------- enroll
    def enroll_embedding(self, person_id: str, name: str, embedding: Sequence[float], *, source: str = "live") -> None:
        """Store an already-computed embedding for ``person_id`` (name display)."""
        vec = _l2_normalize(embedding)
        self._upsert_person(person_id, name)
        self._db.execute(
            "INSERT INTO face_embeddings(person_id, name, embedding, source, created_at) VALUES(?,?,?,?,?)",
            (person_id, name, json.dumps(vec), source, _now_iso()),
        )
        self._db.commit()
        self.metrics["enrollments"] += 1

    def enroll(self, person_id: str, name: str, crop_bgr: Any, *, source: str = "live") -> dict[str, Any]:
        """Detect+embed the face in ``crop_bgr`` and store it. Returns status."""
        vec, box = self.embedder.embed_bgr(crop_bgr)
        if vec is None:
            self.metrics["no_face"] += 1
            return {"enrolled": False, "reason": "no_face"}
        self.enroll_embedding(person_id, name, vec, source=source)
        return {"enrolled": True, "person_id": person_id, "name": name, "face_box": box}

    # ---------------------------------------------------------------- match
    def _gallery(self) -> list[tuple[str, str, list[float]]]:
        rows = self._db.execute("SELECT person_id, name, embedding FROM face_embeddings").fetchall()
        return [(r["person_id"], r["name"], json.loads(r["embedding"])) for r in rows]

    def match_embedding(self, embedding: Sequence[float], *, threshold: float | None = None, top_k: int = 5) -> dict[str, Any]:
        thr = self.config.match_threshold if threshold is None else float(threshold)
        cand = _l2_normalize(embedding)
        # Aggregate per-person best score so several enrolled shots reinforce.
        best_per_person: dict[str, dict[str, Any]] = {}
        for pid, name, vec in self._gallery():
            score = _cosine(cand, vec)
            cur = best_per_person.get(pid)
            if cur is None or score > cur["score"]:
                best_per_person[pid] = {"person_id": pid, "name": name, "score": score}
        scored = sorted(best_per_person.values(), key=lambda x: x["score"], reverse=True)
        out: dict[str, Any] = {
            "person_id": None, "name": None, "score": 0.0, "matched": False,
            "threshold": thr, "method": "face-cosine", "candidates": scored[:top_k],
        }
        if scored:
            top = scored[0]
            matched = top["score"] >= thr
            out.update({
                "person_id": top["person_id"] if matched else None,
                "name": top["name"] if matched else None,
                "score": round(float(top["score"]), 4),
                "matched": matched,
            })
        return out

    def match(self, crop_bgr: Any, *, threshold: float | None = None) -> dict[str, Any]:
        """Detect+embed the face in ``crop_bgr`` and match against the gallery."""
        vec, box = self.embedder.embed_bgr(crop_bgr)
        if vec is None:
            self.metrics["no_face"] += 1
            return {"person_id": None, "name": None, "score": 0.0, "matched": False,
                    "reason": "no_face", "candidates": []}
        res = self.match_embedding(vec, threshold=threshold)
        res["face_box"] = box
        res["query_embedding"] = vec
        if res.get("matched"):
            self.metrics["matches"] += 1
        return res

    def known_names(self) -> dict[str, str]:
        return {r["person_id"]: r["name"] for r in self._db.execute("SELECT person_id, name FROM face_people")}
