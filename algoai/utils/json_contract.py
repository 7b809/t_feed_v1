"""Small JSON Schema validator for deterministic CLI result contracts."""
from __future__ import annotations
from typing import Any, Dict, List

_TYPE_MAP = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}

def validate_json_contract(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    errors: List[str] = []
    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        def matches(kind: str) -> bool:
            pytype = _TYPE_MAP.get(kind)
            if pytype is None:
                return False
            if kind == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            if kind == "number":
                return isinstance(value, (int, float)) and not isinstance(value, bool)
            if kind == "boolean":
                return isinstance(value, bool)
            return isinstance(value, pytype)
        if not any(matches(kind) for kind in allowed):
            return [f"{path}: expected {' or '.join(allowed)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value must be one of {schema['enum']!r}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required field {key!r}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                errors.extend(validate_json_contract(child, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field {key!r}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, child in enumerate(value):
            errors.extend(validate_json_contract(child, schema["items"], f"{path}[{index}]"))
    return errors
