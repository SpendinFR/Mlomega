import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.transport


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


degraded = load("v19_degraded", "services/live-pc/degraded.py")


def _sm():
    return degraded.DegradedStateMachine()


def test_nominal_when_all_signals_healthy():
    sm = _sm()
    state = sm.evaluate(degraded.DegradedSignals(
        now_ts=100.0, heartbeat_ts=99.0, free_vram_mb=4000,
        frame_drops=0, network_latency_ms=20.0,
    ))
    assert state.active is False
    assert state.action_level == degraded.ACTION_NOMINAL
    assert state.event()["type"] == "degraded_state"


def test_pc_absent_when_heartbeat_stale():
    sm = _sm()
    state = sm.evaluate(degraded.DegradedSignals(now_ts=100.0, heartbeat_ts=90.0, free_vram_mb=4000))
    assert state.active is True
    assert "pc_absent" in state.reasons
    assert state.action_level == degraded.ACTION_PC_UNAVAILABLE


def test_gpu_pressure_critical_refuses_vlm():
    sm = _sm()
    state = sm.evaluate(degraded.DegradedSignals(now_ts=100.0, heartbeat_ts=100.0, free_vram_mb=500))
    assert "gpu_pressure_critical" in state.reasons
    assert state.action_level == degraded.ACTION_REFUSE_VLM


def test_gpu_warn_pauses_changes():
    sm = _sm()
    state = sm.evaluate(degraded.DegradedSignals(now_ts=100.0, heartbeat_ts=100.0, free_vram_mb=1200))
    assert "gpu_pressure" in state.reasons
    assert state.action_level == degraded.ACTION_PAUSE_CHANGES


def test_network_degraded_from_drops_and_latency():
    sm = _sm()
    drops = sm.evaluate(degraded.DegradedSignals(now_ts=100.0, heartbeat_ts=100.0, free_vram_mb=4000, frame_drops=100))
    assert "network_degraded" in drops.reasons
    assert drops.action_level == degraded.ACTION_DETECTOR_FLOOR
    lat = sm.evaluate(degraded.DegradedSignals(now_ts=100.0, heartbeat_ts=100.0, free_vram_mb=4000, network_latency_ms=400.0))
    assert "network_degraded" in lat.reasons


def test_worst_action_wins_when_multiple_reasons():
    sm = _sm()
    state = sm.evaluate(degraded.DegradedSignals(
        now_ts=100.0, heartbeat_ts=90.0, free_vram_mb=500, frame_drops=100,
    ))
    # pc_absent is the most severe on the ladder.
    assert state.action_level == degraded.ACTION_PC_UNAVAILABLE
    assert {"pc_absent", "gpu_pressure_critical", "network_degraded"} <= set(state.reasons)
