"""Authorisation guard, shared by the engine and by injected code. Stdlib only.

Grants belong to the human operator, so they live outside the workspace and are
never accepted as a parameter of an AI-facing call.

Policy file:  {"grants": {"web.fetch": ["127.0.0.0/8", "*.example.test"]}}
A pattern is "*", a CIDR or address, an exact host name, or "*.domain".
A plain list of permission names grants each one with no scope limit.
A target may carry a port, such as "127.0.0.1:8000". The grant is for the host.
"""
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import urllib.parse

_PORT = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^:\[\]]+):[0-9]{1,5}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_HOST_NAME = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*")


class NotAuthorized(PermissionError):
    def __init__(self, permission, scope=None):
        detail = f" for {scope}" if scope is not None else ""
        super().__init__(f"permission not granted: {permission}{detail}")
        self.permission = permission
        self.scope = scope


def _policy_path():
    return Path(os.environ.get("DCAP_POLICY") or Path.home() / ".config" / "dcap" / "policy.json")


def normalize(grants):
    """Return {permission: [patterns]}. Anything malformed grants nothing."""
    if isinstance(grants, dict):
        return {
            name: [p for p in patterns if isinstance(p, str)]
            for name, patterns in grants.items()
            if isinstance(patterns, list)
        }
    if isinstance(grants, (list, set, tuple)):
        return {name: ["*"] for name in grants if isinstance(name, str)}
    return {}


_in_use = threading.local()


@contextmanager
def granted(grants):
    """Give code called in this thread the caller's grants instead of the policy file.

    The engine uses this so a capability that makes further checks of its own, such as
    one deciding whether to follow a link, sees the grants its operator gave this run.
    """
    previous = getattr(_in_use, "grants", None)
    _in_use.grants = normalize(grants)
    try:
        yield
    finally:
        _in_use.grants = previous


def load_grants():
    given = getattr(_in_use, "grants", None)
    if given is not None:
        return given
    try:
        data = json.loads(_policy_path().read_text())
    except (OSError, ValueError):
        return {}  # fail closed
    return normalize(data.get("grants")) if isinstance(data, dict) else {}


def scope_values(value):
    """Collect what a value targets: strings, lists, and objects with an address."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [found for item in value for found in scope_values(item)]
    if isinstance(value, dict) and isinstance(value.get("address"), str):
        return [value["address"]]
    return []


def _network(text):
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def host_of(value):
    """The host of a target such as "example.test:8080", "[::1]:8080" or a URL."""
    if _URL.match(value):
        try:
            return urllib.parse.urlsplit(value).hostname or value
        except ValueError:
            return value
    if _PORT.fullmatch(value):
        value = value.rsplit(":", 1)[0]
    return value[1:-1] if value.startswith("[") and value.endswith("]") else value


def covers(pattern, value):
    if pattern == "*":
        return True
    value = host_of(value)
    allowed, wanted = _network(pattern), _network(value)
    if allowed and wanted:
        return allowed.version == wanted.version and wanted.subnet_of(allowed)
    if allowed or wanted:
        return False
    value = value.lower().rstrip(".")
    if not _HOST_NAME.fullmatch(value):
        return False  # a URL, a path or free text is not a name in any granted domain
    pattern = pattern.lower()
    return value.endswith(pattern[1:]) if pattern.startswith("*.") else value == pattern


def authorize(permission, scope=None, grants=None):
    """Raise NotAuthorized unless the permission is granted for everything in scope."""
    patterns = (load_grants() if grants is None else grants).get(permission)
    if patterns is None:
        raise NotAuthorized(permission)
    for value in scope_values(scope):
        if not any(covers(pattern, value) for pattern in patterns):
            raise NotAuthorized(permission, value)
