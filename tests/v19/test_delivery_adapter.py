import pytest
pytestmark = pytest.mark.transport

import importlib.util, sys
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path)); mod = importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod); return mod

delivery_adapter = load('delivery_adapter', 'services/live-pc/delivery_adapter.py')


def test_delivery_row_maps_to_brainlive_context_card_ui_intent():
    intent = delivery_adapter.delivery_row_to_ui_intent({'delivery_id':'d1','message':'hello','action_type':'notify','priority':0.7,'evidence_json':'{"evidence_refs":["x"]}'})
    assert intent.producer == 'brainlive'
    assert intent.component == 'context_card'
    assert intent.delivery_id == 'd1'
    assert intent.evidence_refs == ['x']
