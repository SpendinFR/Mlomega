"""Generate C# POCOs for the V19 contracts from packages/contracts/schemas/*.schema.json.

The committable ``csharp/*.cs`` files are the output of this generator — run it
whenever a schema changes:

    python packages/contracts/generate_csharp.py

Type mapping (JSON Schema -> C#):
    string                          -> string
    number                          -> double
    integer                         -> long
    boolean                         -> bool
    array<string>                   -> List<string>
    array<number|integer|bool>      -> List<double|long|bool>
    array<object>                   -> List<Dictionary<string, object>>
    object (free-form)              -> Dictionary<string, object>
    $ref to a $defs subtype         -> the generated nested class
    anyOf[T, null]                  -> nullable T (value types get '?')
    enum(string)                    -> string  (values documented in a comment)

Property names are PascalCase; each carries [JsonPropertyName("snake_case")].
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
OUT_DIR = Path(__file__).resolve().parent / "csharp"

_VALUE_TYPES = {"double", "long", "bool"}


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def _ref_name(ref: str) -> str:
    # "#/$defs/Pose" -> "Pose"
    return ref.rsplit("/", 1)[-1]


def _scalar_type(schema: dict) -> str | None:
    t = schema.get("type")
    if t == "string":
        return "string"
    if t == "number":
        return "double"
    if t == "integer":
        return "long"
    if t == "boolean":
        return "bool"
    return None


def _array_type(schema: dict) -> str:
    items = schema.get("items") or {}
    if "$ref" in items:
        return f"List<{_ref_name(items['$ref'])}>"
    inner = _scalar_type(items)
    if inner:
        return f"List<{inner}>"
    if items.get("type") == "object":
        return "List<Dictionary<string, object>>"
    return "List<object>"


def _resolve_type(schema: dict) -> tuple[str, bool]:
    """Return (csharp_type, nullable)."""
    if "$ref" in schema:
        return _ref_name(schema["$ref"]), False

    if "anyOf" in schema:
        variants = [v for v in schema["anyOf"] if v.get("type") != "null"]
        nullable = any(v.get("type") == "null" for v in schema["anyOf"])
        if len(variants) == 1:
            inner, _ = _resolve_type(variants[0])
            return inner, nullable
        # Mixed anyOf -> fall back to object.
        return "object", nullable

    if "enum" in schema:
        # Enum may declare an explicit type (e.g. integer rotation 0/90/180/270)
        # or be string-valued. Infer from the type field, then from the values.
        scalar = _scalar_type(schema)
        if scalar:
            return scalar, False
        values = schema.get("enum") or []
        if values and all(isinstance(v, bool) for v in values):
            return "bool", False
        if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return "long", False
        if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            return "double", False
        return "string", False

    t = schema.get("type")
    if t == "array":
        return _array_type(schema), False
    if t == "object":
        return "Dictionary<string, object>", False

    scalar = _scalar_type(schema)
    if scalar:
        return scalar, False
    return "object", False


def _needs_nullable_suffix(cs_type: str) -> bool:
    return cs_type in _VALUE_TYPES


def _render_property(prop_name: str, prop_schema: dict, required: set[str]) -> list[str]:
    cs_type, nullable = _resolve_type(prop_schema)
    # A non-required scalar without a default is nullable so absence is
    # representable; a declared default means the value always exists.
    if prop_name not in required and cs_type in _VALUE_TYPES and prop_schema.get("default") is None:
        nullable = True
    if nullable and _needs_nullable_suffix(cs_type):
        cs_type = cs_type + "?"

    lines: list[str] = []
    comment_bits: list[str] = []
    if "enum" in prop_schema:
        comment_bits.append("enum: " + " | ".join(str(v) for v in prop_schema["enum"]))
    if "default" in prop_schema and prop_schema["default"] is not None:
        comment_bits.append(f"default: {prop_schema['default']}")
    if comment_bits:
        lines.append(f"    // {'; '.join(comment_bits)}")
    lines.append(f'    [JsonPropertyName("{prop_name}")]')
    lines.append(f"    public {cs_type} {_pascal(prop_name)} {{ get; set; }}")
    return lines


def _render_class(name: str, schema: dict, *, nested: dict[str, dict]) -> str:
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}
    lines = [f"public sealed class {name}", "{"]
    first = True
    for prop_name, prop_schema in props.items():
        if not first:
            lines.append("")
        first = False
        lines.extend(_render_property(prop_name, prop_schema, required))
    lines.append("}")

    blocks = ["\n".join(lines)]
    for def_name, def_schema in nested.items():
        blocks.append(_render_class(def_name, def_schema, nested={}))
    return "\n\n".join(blocks)


def generate_one(schema_path: Path) -> Path:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    name = schema.get("title") or schema_path.stem.split(".")[0]
    nested = schema.get("$defs") or {}
    out_path = OUT_DIR / f"{name}.cs"

    header = (
        "// <auto-generated>\n"
        f"// Generated by packages/contracts/generate_csharp.py from schemas/{schema_path.name}\n"
        "// Do not edit by hand; regenerate instead.\n"
        "// </auto-generated>\n"
        "using System.Collections.Generic;\n"
        "using System.Text.Json.Serialization;\n"
        "\n"
        "namespace MLOmega.Contracts.V19;\n\n"
    )
    body = _render_class(name, schema, nested=nested)
    out_path.write_text(header + body + "\n", encoding="utf-8")
    return out_path


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = []
    for schema_path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        generated.append(generate_one(schema_path))
    base = Path(__file__).resolve().parent
    for path in generated:
        try:
            shown = path.relative_to(base)
        except ValueError:
            shown = path
        print(f"wrote {shown}")


if __name__ == "__main__":
    main()
