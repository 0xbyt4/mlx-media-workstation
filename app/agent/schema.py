"""A small JSON-schema checker for tool arguments: the subset the agent tools use, and no new dependency.

Supported: "type" (object, string, integer, number, boolean, array), "properties", "required",
"additionalProperties" (false, or a schema for the extra values), "enum", "minLength", "maxLength", "minimum",
"maximum", "items", "maxItems" and "default". A "type" can be a list ("anyOf" by type). Anything else in a schema is
ignored, so keep the tool schemas inside this subset.

check() returns a new dict: the arguments with the defaults filled in. It raises ArgumentError with a message the
model can act on ("query: expected a string").
"""

from typing import Any


class ArgumentError(ValueError):
    pass


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "null":
        return value is None
    raise ValueError(f"unsupported schema type {kind!r}")


def _check(schema: dict[str, Any], value: Any, where: str) -> Any:
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_type_ok(k, value) for k in kinds):
            # a model may write 5.0 for an integer: accept a whole float as that integer
            if "integer" in kinds and isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ArgumentError(f"{where}: expected {' or '.join(kinds)}")
    if "enum" in schema and value not in schema["enum"]:
        options = ", ".join(repr(o) for o in schema["enum"])
        raise ArgumentError(f"{where}: must be one of {options}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ArgumentError(
                f"{where}: must not be empty"
                if schema["minLength"] == 1
                else f"{where}: at least {schema['minLength']} characters"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ArgumentError(f"{where}: at most {schema['maxLength']} characters")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgumentError(f"{where}: must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgumentError(f"{where}: must be at most {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ArgumentError(f"{where}: at most {schema['maxItems']} items")
        if "items" in schema:
            value = [
                _check(schema["items"], v, f"{where}[{i}]") for i, v in enumerate(value)
            ]
    if isinstance(value, dict):
        value = _check_object(schema, value, where)
    return value


def _check_object(schema: dict[str, Any], value: dict[str, Any], where: str) -> dict:
    props: dict[str, Any] = schema.get("properties", {})
    extra = schema.get("additionalProperties", True)
    out: dict[str, Any] = {}
    for key, v in value.items():
        if not isinstance(key, str):
            raise ArgumentError(f"{where}: keys must be strings")
        path = f"{where}.{key}" if where else key
        if key in props:
            out[key] = _check(props[key], v, path)
        elif extra is False:
            known = ", ".join(props) or "none"
            raise ArgumentError(f"{path}: unknown argument (known: {known})")
        elif isinstance(extra, dict):
            out[key] = _check(extra, v, path)
        else:
            out[key] = v
    for key in schema.get("required", []):
        if key not in out:
            raise ArgumentError(f"{f'{where}.' if where else ''}{key}: required")
    for key, sub in props.items():
        if key not in out and "default" in sub:
            out[key] = sub["default"]
    return out


def check(schema: dict[str, Any], args: Any) -> dict[str, Any]:
    """The arguments checked against an object schema, with defaults filled in."""
    if not isinstance(args, dict):
        raise ArgumentError("the arguments must be a JSON object")
    return _check_object(schema, args, "")
