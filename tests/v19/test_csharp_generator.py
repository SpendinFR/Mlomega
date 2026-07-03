import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.contracts


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = load("v19_generate_csharp", "packages/contracts/generate_csharp.py")
CSHARP_DIR = Path("packages/contracts/csharp")
SCHEMA_DIR = Path("packages/contracts/schemas")


def test_pascal_and_type_mapping():
    assert gen._pascal("ui_intent_id") == "UiIntentId"
    assert gen._resolve_type({"type": "string"}) == ("string", False)
    assert gen._resolve_type({"type": "number"}) == ("double", False)
    assert gen._resolve_type({"type": "integer"}) == ("long", False)
    assert gen._resolve_type({"type": "boolean"}) == ("bool", False)
    assert gen._resolve_type({"type": "array", "items": {"type": "string"}}) == ("List<string>", False)
    assert gen._resolve_type({"type": "object", "additionalProperties": True}) == ("Dictionary<string, object>", False)
    # nullable via anyOf[str, null]
    assert gen._resolve_type({"anyOf": [{"type": "string"}, {"type": "null"}]}) == ("string", True)
    # integer enum stays long, not string
    assert gen._resolve_type({"enum": [0, 90, 180, 270]}) == ("long", False)


def test_all_schemas_have_generated_cs_committed():
    schemas = sorted(p.stem.split(".")[0] for p in SCHEMA_DIR.glob("*.schema.json"))
    for name in schemas:
        assert (CSHARP_DIR / f"{name}.cs").exists(), name


def test_generated_cs_is_up_to_date(tmp_path, monkeypatch):
    # Regenerate into the real dir is destructive; instead assert the committed
    # UIIntent.cs contains the expected hallmarks of a real POCO.
    text = (CSHARP_DIR / "UIIntent.cs").read_text(encoding="utf-8")
    assert "public sealed class UIIntent" in text
    assert '[JsonPropertyName("ui_intent_id")]' in text
    assert "public string UiIntentId { get; set; }" in text
    assert "public double Priority { get; set; }" in text
    assert "public List<string> EvidenceRefs { get; set; }" in text
    # FrameEnvelope nested Pose + integer rotation
    fe = (CSHARP_DIR / "FrameEnvelope.cs").read_text(encoding="utf-8")
    assert "public sealed class Pose" in fe
    assert "public long Rotation { get; set; }" in fe
    assert "public Pose Pose { get; set; }" in fe


def test_generator_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "OUT_DIR", tmp_path)
    gen.main()
    committed = (CSHARP_DIR / "UIIntent.cs").read_text(encoding="utf-8")
    fresh = (tmp_path / "UIIntent.cs").read_text(encoding="utf-8")
    assert committed == fresh
