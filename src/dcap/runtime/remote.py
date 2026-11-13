"""Remote provider calls over MCP stdio or HTTP. Stdlib only."""
from contextlib import contextmanager
import json
import os
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request


class RemoteError(RuntimeError):
    pass


def _rpc(proc, msg_id, method, params):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RemoteError("provider closed the connection")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RemoteError("provider sent a malformed message")
        if message.get("id") == msg_id:
            if "error" in message:
                raise RemoteError(str(message["error"]))
            if "result" not in message:
                raise RemoteError("provider sent a reply with no result")
            return message["result"]


@contextmanager
def _mcp_session(command, timeout):
    """One MCP stdio session. The timer kills a provider that stops responding."""
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        _rpc(proc, 1, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "dcap", "version": "0.1.0"},
        })
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        yield proc
    except (OSError, ValueError) as exc:
        raise RemoteError(str(exc)) from exc
    finally:
        timer.cancel()
        proc.kill()
        proc.communicate()


def list_tools(command, timeout=30):
    with _mcp_session(command, timeout) as proc:
        result = _rpc(proc, 2, "tools/list", {})
    tools = result.get("tools", []) if isinstance(result, dict) else None
    if not isinstance(tools, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools):
        raise RemoteError("provider returned a malformed tool list")
    return tools


def _map_inputs(spec, inputs):
    return {spec.get("input_map", {}).get(k, k): v for k, v in inputs.items() if v is not None}


def _call_mcp(spec, inputs, timeout):
    with _mcp_session(spec["command"], timeout) as proc:
        # Some servers keep state, so tools a call depends on run first in the same session.
        message_id, consumed = 2, set()
        for step in spec.get("before", []):
            mapping = step.get("input_map", {})
            consumed |= set(mapping)
            # A definition can name inputs that are paths, for servers that need them absolute.
            arguments = {
                mapping[k]: os.path.abspath(inputs[k]) if k in step.get("paths", ()) else inputs[k]
                for k in mapping if inputs.get(k) is not None
            }
            if _rpc(proc, message_id, "tools/call", {"name": step["tool"], "arguments": arguments}).get("isError"):
                raise RemoteError("provider reported a tool error")
            message_id += 1
        remaining = {k: v for k, v in inputs.items() if k not in consumed}
        result = _rpc(proc, message_id, "tools/call", {"name": spec["tool"], "arguments": _map_inputs(spec, remaining)})
    if result.get("isError"):
        raise RemoteError("provider reported a tool error")
    text = next((c["text"] for c in result.get("content", []) if c.get("type") == "text"), None)
    mode = spec.get("text_output")
    if mode is True:  # declared by the provider definition, never guessed from the reply
        if text is None:
            raise RemoteError("provider returned no usable content")
        return text
    reply = result.get("structuredContent")
    if reply is None:
        if text is None:
            raise RemoteError("provider returned no usable content")
        if mode == "auto":  # a plugged server with no declared output: data if it is data, else its text
            try:
                return json.loads(text)
            except ValueError:
                return text
        reply = json.loads(text)
    return reply


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # credentials must never follow a redirect to another host


def _call_http(spec, inputs, timeout):
    arguments = _map_inputs(spec, inputs)
    method = spec.get("method", "POST").upper()
    url, data = spec["url"], None
    if method == "GET":
        url = f"{url}?{urllib.parse.urlencode(arguments, doseq=True)}"
    else:
        data = json.dumps(arguments).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        # Secrets are named by environment variable and never stored in definitions.
        headers.update({name: os.environ[var] for name, var in spec.get("headers_env", {}).items()})
    except KeyError as exc:
        raise RemoteError(f"missing environment variable {exc}") from exc
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RemoteError(f"provider returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RemoteError(str(exc)) from exc


def call_remote(spec, inputs, timeout=30):
    """Call the provider described by spec and return its normalised output."""
    calls = {"mcp": _call_mcp, "http": _call_http}
    reply = calls[spec["kind"]](spec, inputs, timeout)
    key = spec.get("output_key")
    if key is None:
        return reply
    if not isinstance(reply, dict) or key not in reply:
        raise RemoteError(f"provider output has no '{key}'")
    return reply[key]
