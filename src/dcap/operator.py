"""Who is asking. Operator commands answer a person at a terminal, or the holder of the operator key.

An AI agent's shell has no terminal, so an agent that finds the `dcap` command gets nothing from it.
For scripts, the operator makes a key with `dcap operator-key` and passes it as DCAP_OPERATOR.
"""
import hmac
import os
from pathlib import Path
import secrets
import sys


def _home():
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)  # not $HOME, which a caller can point anywhere
    except (ImportError, KeyError):
        return Path.home()


def key_file():
    return _home() / ".config" / "dcap" / "operator.key"


def at_terminal():
    for stream in (sys.stdin, sys.stderr):
        try:
            if stream is not None and stream.isatty():
                return True
        except (AttributeError, ValueError):
            pass
    return False


def holds_key():
    given = os.environ.get("DCAP_OPERATOR", "")
    if not given:
        return False
    try:
        stored = key_file().read_text().strip()
    except OSError:
        return False
    return bool(stored) and hmac.compare_digest(given.encode(), stored.encode())


def present():
    return at_terminal() or holds_key()


def make_key():
    """Write a new operator key, readable only by this user. Returns it."""
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(24)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as out:
        out.write(key + "\n")
    os.chmod(path, 0o600)
    return key
