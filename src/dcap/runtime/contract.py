"""Capability contract checks, shared by the engine and by injected code. Stdlib only."""
import re

_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate(value, schema, types, path="$"):
    """Return a list of problems; an empty list means the value is valid."""
    if "ref" in schema:
        return validate(value, types[schema["ref"]], types, path)
    kind = schema.get("type")
    if kind and not _CHECKS[kind](value):
        return [f"{path}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: not one of {schema['enum']}"]
    if "pattern" in schema and not (isinstance(value, str) and re.fullmatch(schema["pattern"], value)):
        return [f"{path}: not a valid value"]
    problems = []
    if kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            problems += validate(item, schema["items"], types, f"{path}[{index}]")
    if kind == "object":
        problems += [f"{path}.{name}: required" for name in schema.get("required", []) if name not in value]
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                problems += validate(value[name], sub, types, f"{path}.{name}")
    return problems


def conform(value, schema, types):
    """Keep only what a type declares. A type with no declared properties stays open."""
    if "ref" in schema:
        return conform(value, types[schema["ref"]], types)
    if schema.get("type") == "array" and "items" in schema and isinstance(value, list):
        return [conform(item, schema["items"], types) for item in value]
    if schema.get("type") == "object" and "properties" in schema and isinstance(value, dict):
        declared = schema["properties"]
        return {name: conform(item, declared[name], types) for name, item in value.items() if name in declared}
    return value


class CapabilityError(RuntimeError):
    """A standard failure from injected code. It never carries provider details."""

    def __init__(self, code, capability, message):
        super().__init__(f"{code}: {capability}: {message}")
        self.code = code
        self.capability = capability


def checked_call(capability, types, inputs, output, call, retries=1):
    """Run one capability from injected code with the engine's contract checks.

    inputs maps a name to (type name, value). Failover stays with the engine, because
    only the selected implementation is copied into a workspace.
    """
    problems = []
    for name, (type_name, value) in inputs.items():
        if value is not None:
            problems += validate(value, types[type_name], types, name)
    if problems:
        raise CapabilityError("INVALID_INPUT", capability, "; ".join(problems))
    code = "EXECUTION_FAILED"
    for _ in range(retries + 1):
        try:
            value = call()
        except Exception:  # provider faults stay behind the interface
            code = "EXECUTION_FAILED"
            continue
        if validate(value, types[output], types):
            code = "INVALID_OUTPUT"
            continue
        return conform(value, types[output], types)
    raise CapabilityError(code, capability, "the provider could not complete this capability")
