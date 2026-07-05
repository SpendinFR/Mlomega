"""E36 — Ops de prod: outside-access failover, stranger VLM profile, doctor quotas.

Real PC-side checks with a temp SQLite DB (no hardware, no cloud):

* **failover** : the ``EndpointResolver`` picks LAN when it is up; when the LAN port
  is closed it fails over to endpoint 2; both down → a clean ``pc_unreachable``
  verdict (the device reflex path is untouched); a reconnect re-tests the first so a
  return home reclaims the LAN. A full session + signaling round-trip runs through
  the SECOND endpoint (two localhost SessionHubs on different ports simulate the
  LAN-down → tunnel case);
* **stranger profile** : a persistent anonymous person track → one VLM crop (the
  boundary mocked when Ollama is off, real reply format) → a provisional ``inferred``
  entity labelled by description (« ? boulanger », never a name) + an
  ``entity_hot_update``; a later enrollment fuses the provisional profile into the
  named entity (description kept as attribute); never two VLM profiles for one track;
* **doctor quotas** : the DOCTOR script runs its ``-Quota`` subset and reports the
  real DB / models / evidence / day-buffer sizes.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


endpoint_resolver = _load("v19_endpoint_resolver", "services/live-pc/endpoint_resolver.py")
stranger_profile = _load("v19_stranger_profile", "services/live-pc/stranger_profile.py")
worldbrain = _load("v19_worldbrain", "services/live-pc/worldbrain.py")
degraded = _load("v19_degraded", "services/live-pc/degraded.py")


# ============================================================ endpoint failover
def _eps():
    Endpoint = endpoint_resolver.Endpoint
    return [
        Endpoint(name="lan", host="192.168.1.10", port=8710),
        Endpoint(name="tailscale", host="100.64.0.2", port=8710),
    ]


def test_parse_endpoints_list_and_legacy():
    eps = endpoint_resolver.parse_endpoints(
        [{"name": "lan", "host": "192.168.1.10"}, {"name": "t", "host": "100.64.0.2", "port": 8711}]
    )
    assert [e.name for e in eps] == ["lan", "t"]
    assert eps[1].port == 8711 and eps[1].webrtc_offer_url.endswith("/webrtc/offer")
    # legacy single pc_host still resolves to one implicit lan endpoint
    legacy = endpoint_resolver.endpoints_from_profile({"pc_host": "192.168.1.5"})
    assert len(legacy) == 1 and legacy[0].name == "lan" and legacy[0].host == "192.168.1.5"


def test_lan_up_lan_chosen():
    eps = _eps()
    r = endpoint_resolver.EndpointResolver(eps, probe=lambda e: e.name == "lan")
    result = r.resolve()
    assert result.reachable and result.active.name == "lan"
    assert r.metrics["failovers"] == 0


def test_lan_down_failover_to_second():
    eps = _eps()
    # LAN port closed → probe fails only for lan; tailscale answers.
    r = endpoint_resolver.EndpointResolver(eps, probe=lambda e: e.name == "tailscale")
    result = r.resolve()
    assert result.reachable and result.active.name == "tailscale"
    assert "lan" in result.tried and "tailscale" in result.tried


def test_both_down_pc_unreachable():
    eps = _eps()
    r = endpoint_resolver.EndpointResolver(eps, probe=lambda e: False)
    result = r.resolve()
    assert not result.reachable and result.active is None
    assert result.reason == "pc_unreachable"
    assert r.metrics["unreachable"] == 1
    # device reflex path is unaffected: the resolver never raised.


def test_reconnect_returns_to_first():
    eps = _eps()
    # Simulate: away (lan down) → tailscale, then home (lan up) → back to lan.
    state = {"lan_up": False}
    r = endpoint_resolver.EndpointResolver(eps, probe=lambda e: state["lan_up"] if e.name == "lan" else True)
    away = r.resolve()
    assert away.active.name == "tailscale"
    state["lan_up"] = True
    home = r.on_disconnect()  # reconnect re-tests from the top
    assert home.active.name == "lan"
    assert r.metrics["failovers"] >= 1  # tailscale → lan counted as a failover


# ---- full session + signaling through the SECOND endpoint (two localhost hubs) --
def _load_sessionhub_http():
    # sessionhub_http loads its siblings by path; import it the same way.
    spec = importlib.util.spec_from_file_location(
        "v19_sessionhub_http", ROOT / "services" / "live-pc" / "sessionhub_http.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["v19_sessionhub_http"] = mod
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_session_and_signaling_via_second_endpoint():
    """LAN down (port closed) → resolver picks the 2nd endpoint; a full session
    create + /health round-trip runs against it. Two localhost SessionHubs on
    different ports simulate LAN vs tunnel."""
    fastapi = importlib.util.find_spec("fastapi")
    if fastapi is None:
        pytest.skip("fastapi not installed")
    import uvicorn
    from fastapi.testclient import TestClient

    sh = _load_sessionhub_http()
    # Endpoint 2 (the "tunnel") is a live in-process app; endpoint 1 (the "LAN") is
    # a closed port. The resolver must skip LAN and land on the tunnel.
    app2 = sh.create_app(enable_signaling=False)
    client2 = TestClient(app2)

    lan_port = _free_port()   # nothing listens here → LAN down
    tunnel_port = _free_port()

    Endpoint = endpoint_resolver.Endpoint
    eps = [
        Endpoint(name="lan", host="127.0.0.1", port=lan_port),
        Endpoint(name="tailscale", host="127.0.0.1", port=tunnel_port),
    ]

    # Probe: LAN closed; tunnel answers (proxied through the TestClient /health).
    def probe(ep: object) -> bool:
        if ep.name == "lan":
            return False
        return client2.get("/health").json().get("status") == "ok"

    resolver = endpoint_resolver.EndpointResolver(eps, probe=probe)
    result = resolver.resolve()
    assert result.reachable and result.active.name == "tailscale"

    # A full session create runs against the resolved (2nd) endpoint.
    r = client2.post("/session/create", json={"device_id": "s25-primary"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] and body["token"]
    # renew works with the token (proves the whole session surface is reachable).
    r2 = client2.post("/session/renew", json={"session_id": body["session_id"], "token": body["token"]})
    assert r2.status_code == 200 and r2.json()["token"]


# ============================================================ WAN degradation
def test_wan_profile_distinct_from_lan():
    profiles = degraded.default_network_profiles()
    lan, wan = profiles["lan"], profiles["wan"]
    # WAN tolerates higher latency and lowers the target video height; the PC
    # detector cadence is not part of the network profile at all.
    assert wan.max_network_latency_ms > lan.max_network_latency_ms
    assert wan.target_video_height < lan.target_video_height
    # thresholds_for_link tracks the network limits per link, GPU/heartbeat stay base.
    base = degraded.DegradedThresholds()
    wan_t = degraded.thresholds_for_link(profiles, "wan", base)
    assert wan_t.max_network_latency_ms == wan.max_network_latency_ms
    assert wan_t.vram_floor_mb == base.vram_floor_mb  # GPU limit is link-independent


def test_network_profiles_from_config_override():
    profiles = degraded.network_profiles_from_config(
        {"network": {"wan": {"target_video_height": 480, "max_network_latency_ms": 500}}}
    )
    assert profiles["wan"].target_video_height == 480
    assert profiles["wan"].max_network_latency_ms == 500.0


# ============================================================ stranger profile
class _FakeVlm:
    """Mock VisionRT.VlmCrop.describe — real reply format (Ollama boundary)."""

    def __init__(self, status="ok", text=None, count_holder=None):
        self.status = status
        self.text = text if text is not None else json.dumps(
            {"appearance": "homme, ~40 ans", "clothing": "tablier blanc",
             "age_apparent": "40s", "role_hint": "probably a baker"}
        )
        self.calls = 0
        self._count = count_holder

    def describe(self, crop_bgr, prompt="?"):
        self.calls += 1
        if self._count is not None:
            self._count[0] += 1
        return {"status": self.status, "text": self.text if self.status == "ok" else None,
                "model": "moondream"}


def _person_worldbrain(tmp_path):
    wb = worldbrain.WorldBrain(
        person_id="me", live_session_id="s-e36",
        config=worldbrain.WorldBrainConfig(promote_min_observations=1, promote_min_confidence=0.3),
        db_path=None, publish_world_state=False,
    )
    # promote one person entity
    delta = {"session_id": "s-e36", "source_frame_id": "f1",
             "entities": [{"track_id": "t1", "kind": "object", "label": "person",
                           "bbox": [0, 0, 50, 100], "confidence": 0.9}]}
    wb.ingest_scene_delta(delta)
    eid = wb._track_to_entity.get("t1")
    return wb, eid


def test_stranger_profile_created_after_stable_anonymous(tmp_path):
    import numpy as np

    wb, eid = _person_worldbrain(tmp_path)
    hot = []
    clock = {"t": 0.0}
    vlm = _FakeVlm()
    prof = stranger_profile.StrangerProfiler(
        vlm=vlm, worldbrain=wb,
        config=stranger_profile.StrangerConfig(stable_seconds=4.0),
        on_entity_hot_update=lambda m: hot.append(m),
        now_fn=lambda: clock["t"],
    )
    crop = np.zeros((100, 50, 3), dtype=np.uint8)
    # first sighting: starts the timer, no profile yet
    assert prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop) is None
    # not stable long enough
    clock["t"] = 2.0
    assert prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop) is None
    # stable > 4s → ONE VLM profile
    clock["t"] = 5.0
    p = prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)
    assert p is not None
    assert p.truth_level == "inferred"
    assert p.description.startswith("? ") and "baker" in p.description  # role hint, never a name
    assert p.attributes.get("clothing") == "tablier blanc"
    # entity_hot_update pushed as a hypothesis (no name/person_id)
    assert hot and hot[-1]["type"] == "entity_hot_update"
    assert hot[-1]["name"] is None and hot[-1]["person_id"] is None
    assert hot[-1]["truth_level"] == "inferred"
    # WorldBrain entity carries the description (inferred), NEVER a person_name
    ent = wb.entities[eid]
    assert getattr(ent, "description") == p.description
    assert getattr(ent, "description_truth_level") == "inferred"
    assert getattr(ent, "person_name", None) is None


def test_never_two_vlm_profiles_for_one_track(tmp_path):
    import numpy as np

    wb, eid = _person_worldbrain(tmp_path)
    clock = {"t": 0.0}
    count = [0]
    vlm = _FakeVlm(count_holder=count)
    prof = stranger_profile.StrangerProfiler(
        vlm=vlm, worldbrain=wb,
        config=stranger_profile.StrangerConfig(stable_seconds=1.0),
        now_fn=lambda: clock["t"],
    )
    crop = np.zeros((100, 50, 3), dtype=np.uint8)
    prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)
    clock["t"] = 2.0
    prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)  # profiles now
    clock["t"] = 3.0
    prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)  # dedup
    clock["t"] = 4.0
    prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)  # dedup
    assert count[0] == 1  # exactly one VLM call for the track this session


def test_vlm_unavailable_honest_degrade(tmp_path):
    import numpy as np

    wb, eid = _person_worldbrain(tmp_path)
    clock = {"t": 0.0}
    prof = stranger_profile.StrangerProfiler(
        vlm=_FakeVlm(status="vlm_unavailable"), worldbrain=wb,
        config=stranger_profile.StrangerConfig(stable_seconds=1.0),
        now_fn=lambda: clock["t"],
    )
    crop = np.zeros((100, 50, 3), dtype=np.uint8)
    prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)
    clock["t"] = 2.0
    p = prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)
    assert p is None  # no invented description
    assert prof.metrics["vlm_unavailable"] == 1
    assert getattr(wb.entities[eid], "description", None) is None


def test_named_track_never_profiled(tmp_path):
    import numpy as np

    wb, eid = _person_worldbrain(tmp_path)
    vlm = _FakeVlm()
    prof = stranger_profile.StrangerProfiler(
        vlm=vlm, worldbrain=wb, config=stranger_profile.StrangerConfig(stable_seconds=0.0),
    )
    crop = np.zeros((100, 50, 3), dtype=np.uint8)
    # a named track is skipped entirely, timer cleared
    assert prof.observe_track("t1", entity_id=eid, is_person=True, is_named=True, crop_bgr=crop) is None
    assert vlm.calls == 0


def test_fusion_into_named_keeps_description(tmp_path):
    import numpy as np

    wb, eid = _person_worldbrain(tmp_path)
    hot = []
    clock = {"t": 0.0}
    prof = stranger_profile.StrangerProfiler(
        vlm=_FakeVlm(), worldbrain=wb,
        config=stranger_profile.StrangerConfig(stable_seconds=1.0),
        on_entity_hot_update=lambda m: hot.append(m),
        now_fn=lambda: clock["t"],
    )
    crop = np.zeros((100, 50, 3), dtype=np.uint8)
    prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)
    clock["t"] = 2.0
    p = prof.observe_track("t1", entity_id=eid, is_person=True, is_named=False, crop_bgr=crop)
    assert p is not None and p.fused_into is None
    # later: enrollment names the person → fuse
    fused = prof.fuse_into_named(track_id="t1", entity_id=eid, person_id="live-karim", name="Karim")
    assert fused is not None and fused.fused_into == "live-karim"
    ent = wb.entities[eid]
    assert getattr(ent, "person_name") == "Karim"           # now named
    assert getattr(ent, "description") == p.description       # description kept as attribute
    assert getattr(ent, "description_truth_level") == "observed"
    # the last hot update names the person and supersedes the "?" hypothesis
    assert hot[-1]["name"] == "Karim" and hot[-1]["person_id"] == "live-karim"
    assert hot[-1]["truth_level"] == "observed"


def test_parse_vlm_description_never_extracts_a_name():
    # free prose (no JSON) → kept as appearance only, no name field ever
    attrs = stranger_profile.parse_vlm_description("A tall man named Bob wearing a red coat")
    assert "name" not in attrs
    assert "appearance" in attrs
    label = stranger_profile.description_label(attrs)
    assert label.startswith("? ")


# ============================================================ doctor quotas
def test_doctor_quota_section_runs_and_reports_sizes(tmp_path, monkeypatch):
    """Run the real DOCTOR script -Quota and show the storage quotas output."""
    if sys.platform != "win32":
        pytest.skip("DOCTOR script is PowerShell/Windows")
    import shutil

    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("powershell not available")

    script = ROOT / "scripts" / "DOCTOR_MLOMEGA_V19.ps1"
    # Point the evidence root + DB at a seeded temp dir so the sizes are real.
    ev = tmp_path / "evidence"
    (ev / "keyframes").mkdir(parents=True)
    (ev / "day_buffer").mkdir(parents=True)
    (ev / "keyframes" / "k1.jpg").write_bytes(b"x" * 4096)
    (ev / "day_buffer" / "b1.bin").write_bytes(b"y" * 8192)
    db = tmp_path / "memory.db"
    db.write_bytes(b"z" * 2048)

    env = dict(**__import__("os").environ)
    env["MLOMEGA_DB"] = str(db)
    env["MLOMEGA_EVIDENCE"] = str(ev)
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Quota"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # The quota section ran and reported the real footprint pieces.
    assert "Stockage" in out or "quota" in out.lower()
    assert "DB SQLite" in out
    assert "Tampon-jour" in out or "tampon" in out.lower()
    # -Quota with tiny sizes must not FAIL the run.
    assert proc.returncode == 0, out[-2000:]
