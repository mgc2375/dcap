"""Keep an AI on its side of DCAP.

The AI may read and run what DCAP pasted into its workspace. It may not ask DCAP what else
exists: not through the command line, the Python package, the definitions, the policy or
DCAP's own state. An agent's adapter calls `refuses` before one of the agent's tools runs.
"""
import os
from pathlib import Path
import re

from . import operator, plug
from .runtime.policy import _policy_path

REFUSAL = "That isn't available here."

_WRAPPER = r"(?:(?:env|exec|command|nohup|time|sudo|xargs|script)\s+(?:-\S+\s+|/dev/null\s+)*)"
_ASSIGN = r"(?:\w+=\S*\s+)"
_COMMAND = [
    # the command itself, wherever it is installed
    re.compile(rf"(?:^|[;&|(`]|\$\()\s*{_ASSIGN}*(?:{_WRAPPER}{_ASSIGN}*)*(?:\S*/)?dcap(?=\s|$|[;&|)])", re.MULTILINE),
    # the Python package, by module or by import
    re.compile(r"-m\s+dcap(?![\w-])"),
    re.compile(r"\b(?:import|from)\s+dcap(?!\w)"),
    re.compile(r"""["']dcap(?:\.[\w.]+)?["']"""),
    # DCAP's state, its installed files and the operator's settings
    re.compile(r"(?<![\w-])\.dcap(?![\w-])"),
    re.compile(r"/dcap/(?:definitions|runtime|[\w-]+\.(?:py|ya?ml|md))"),
    re.compile(r"\.config/dcap(?![\w-])"),
    re.compile(r"\bDCAP_(?:POLICY|OPERATOR|PLUGGED)\b"),
]
_IMPORTS = [_COMMAND[2], _COMMAND[3]]
_STATE = _COMMAND[4]

_COMMAND_KEYS = {"command", "cmd", "script"}
_CONTENT_KEYS = {"content", "new_string", "newstring", "new_str", "new_source"}
_PATTERN_KEYS = {"pattern", "glob", "include"}


def _real(path, base):
    path = Path(os.path.expanduser(str(path)))
    return Path(os.path.realpath(path if path.is_absolute() else Path(base) / path))


def _inside(path, parent):
    return path == parent or parent in path.parents


def protected(workspace, definitions=()):
    """The places an AI has no business in, as real paths."""
    workspace = _real(workspace, ".")
    package = Path(os.path.realpath(Path(__file__).parent))
    places = [package, *(_real(d, ".") for d in definitions), _real(operator.key_file().parent, "."),
              _real(_policy_path(), "."), _real(plug.store(), ".")]
    checkout = package.parents[1]
    if (checkout / "pyproject.toml").is_file() and package.parent.name == "src":
        places.append(checkout)  # run from source, the docs and tests sit beside the package
    # A workspace inside one of these is the operator working on DCAP itself.
    return [workspace / ".dcap", *(p for p in places if not _inside(workspace, p))]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def refuses(tool_input, workspace, definitions=(), cwd=None):
    """True if running this tool input would take the AI behind DCAP.

    Works on the input alone, so it suits any agent: a key naming a command is checked as a
    command, one naming a path as a path, and one holding new file content as content.
    """
    if not isinstance(tool_input, dict):
        return False
    places = protected(workspace, definitions)
    base = cwd or workspace
    for key, value in tool_input.items():
        name = key.lower()
        if name in _COMMAND_KEYS and isinstance(value, str):
            if any(rule.search(value) for rule in _COMMAND) or any(str(place) in value for place in places):
                return True
        elif ("path" in name or name in {"file", "filename"}) and isinstance(value, str) and value:
            if any(_inside(_real(value, base), place) for place in places):
                return True
        elif name in _PATTERN_KEYS and isinstance(value, str):
            if _STATE.search(value) or any(str(place) in value for place in places):
                return True
        elif name in _CONTENT_KEYS or name == "edits":
            if any(rule.search(text) for text in _strings(value) for rule in _IMPORTS):
                return True
    return False
