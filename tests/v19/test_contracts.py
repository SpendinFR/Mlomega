import pytest
pytestmark = pytest.mark.contracts

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pytest
from packages.contracts.python.models import *

CASES = [
    FrameEnvelope(session_id='s', frame_id='f1', capture_monotonic_ns=1, captured_at_utc='2026-07-03T00:00:00Z', pose=Pose(), source='sim'),
    LocalTrack(session_id='s', track_id='t', source_frame_id='f1', kind='object', bbox_or_mask={'x':1}, visibility=1.0, confidence=.9, observed_at_monotonic_ns=1),
    SceneDelta(session_id='s', source_frame_id='f1'),
    ReflexEvent(session_id='s', source_frame_id='f1', skill='MotionProximity', prediction={}, horizon_ms=100, confidence=.8, severity='info', aggregate_key='k'),
    UIIntent(ui_intent_id='u', producer='brainlive', component='context_card', anchor={}, content={}, truth_level='observed', confidence=.9, priority=1, ttl_ms=1000),
    UIReceipt(ui_intent_id='u', event='displayed', observed_at='2026-07-03T00:00:00Z', source='test'),
    HotSceneContext(session_id='s', as_of='2026-07-03T00:00:00Z'),
    EvidenceEvent(event_type='visual', occurred_at='2026-07-03T00:00:00Z', session_id='s', observation={}, truth_level='observed', confidence=1.0, provenance={}),
]

def test_contract_round_trips():
    for item in CASES:
        clone = item.__class__.model_validate_json(item.model_dump_json())
        assert clone == item
        assert clone.contracts_version == 'v19.0'

def test_contracts_are_strict():
    with pytest.raises(Exception):
        FrameEnvelope.model_validate({'session_id':'s','frame_id':'f','capture_monotonic_ns':1,'captured_at_utc':'x','pose':{},'source':'sim','unexpected':True})
