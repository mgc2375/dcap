"""Command line for the operator. `hook` is how DCAP listens to an agent."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys

from . import guard, operator, plug
from .api import Dcap
from .errors import DcapError
from .listener import Listener, claude_code_hook
from .registry import Registries
from .resolver import Resolver
from .runtime.remote import RemoteError, list_tools
from .scaffold import draft, render


def _params(pairs):
    out = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def _grants(items):
    """'perm' grants without limit, 'perm=10.0.0.0/8' grants that scope."""
    if items is None:
        return None
    grants = {}
    for item in items:
        name, _, scope = item.partition("=")
        grants.setdefault(name, []).append(scope or "*")
    return grants


def _definitions(args):
    """Every plugged server, then whatever the operator named for this command."""
    return [*map(str, plug.folders()), *args.definitions]


def _renames(pairs):
    return dict(pair.split("=", 1) for pair in pairs or [] if "=" in pair)


def _command(values):
    return values[1:] if values[:1] == ["--"] else values


def _context(args):
    """What a server's tool list would cost an AI. DCAP itself adds no tool definition."""
    command = _command(args.server)
    if not command:
        return {"dcap_tool_definition_bytes": 0}
    tools = list_tools(command)
    other = len(json.dumps(tools))
    return {"dcap_tool_definition_bytes": 0, "server": {"tools": len(tools), "bytes": other, "approx_tokens": other // 4}}


_DENY = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": guard.REFUSAL,
}}


def _refused(tool_input, workspace, definitions, cwd=None):
    """Whether the guard turns a tool call away. A fault in the guard turns it away too."""
    try:
        refused = guard.refuses(tool_input, workspace, definitions, cwd)
    except Exception as exc:
        _log_failure(workspace, exc)
        return True
    if refused:
        _log_failure(workspace, PermissionError("guard: turned away a tool call that reached behind DCAP"))
    return refused


def _hook(args):
    """Listen to an agent's native activity. Stdout is only ever what the AI should see."""
    workspace = args.workspace
    try:
        event = json.load(sys.stdin)
        if workspace == ".":
            workspace = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or workspace
        if event.get("hook_event_name") == "PreToolUse":
            if _refused(event.get("tool_input"), workspace, _definitions(args), event.get("cwd")):
                print(json.dumps(_DENY))
            return 0
        output = claude_code_hook(Dcap(workspace, _definitions(args), simulate=args.simulate), event)
    except Exception as exc:
        _log_failure(workspace, exc)
        return 0
    if output:
        print(json.dumps(output))
    return 0


def _log_failure(workspace, exc):
    try:
        log = Path(workspace) / ".dcap" / "door.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as handle:
            handle.write(f"{type(exc).__name__}: {exc}\n")
    except OSError:
        pass


def _listen(args):
    """Any agent's adapter: {"said": [...], "wrote": [...]} in, what the AI should see out."""
    try:
        event = json.load(sys.stdin)
        listener = Listener(Dcap(args.workspace, _definitions(args), simulate=args.simulate))
        message = listener.observe(event.get("said", []), event.get("wrote", []))
        listener.save()
    except Exception as exc:
        _log_failure(args.workspace, exc)
        return 0
    if message:
        print(message)
    return 0


def _guard(args):
    """Any agent's adapter, before a tool runs: {"input": {...}} in, exit code 2 if it must not run."""
    try:
        event = json.load(sys.stdin)
        tool_input = event.get("input")
    except Exception as exc:
        _log_failure(args.workspace, exc)
        return 2
    return 2 if _refused(tool_input, args.workspace, _definitions(args), event.get("cwd")) else 0


_AGENT_SIDE = {"hook", "listen", "guard"}


def _command_of(argv):
    """The subcommand in argv, skipping the options that come before it."""
    index = 0
    while index < len(argv):
        word = argv[index]
        if word in {"--workspace", "--definitions", "--grant"}:
            index += 2
        elif word == "--simulate" or word.startswith(("--workspace=", "--definitions=", "--grant=")):
            index += 1
        else:
            return word
    return None


def main(argv=None):
    """Operator commands answer only an operator. Anyone else gets silence, not an explanation."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if operator.present():
        return _run(argv)
    if _command_of(argv) not in _AGENT_SIDE or {"-h", "--help"} & set(argv):
        return 1
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return _run(argv)
    except SystemExit:
        return 1


def _run(argv):
    parser = argparse.ArgumentParser(prog="dcap")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--definitions", action="append", default=[], help="extra registry directory")
    parser.add_argument("--grant", action="append", default=None,
                        help="grant for this command: permission or permission=scope")
    parser.add_argument("--simulate", action="store_true", default=None,
                        help="use canned data instead of live providers (also DCAP_SIMULATE=1)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hook", help="listen to an agent's activity, from a Claude Code hook")
    sub.add_parser("listen", help="listen to an agent's activity, from any other adapter")
    sub.add_parser("guard", help="check a tool call before an agent makes it, from any other adapter")
    sub.add_parser("operator-key", help="make the key that lets a script run operator commands")

    request = sub.add_parser("request", help="make a request and see the full operator view")
    request.add_argument("text")
    request.add_argument("--param", action="append", help="name=value")
    request.add_argument("--target", help="existing symbol to connect")
    request.add_argument("--no-execute", action="store_true")

    explain = sub.add_parser("explain", help="show how a request resolves, without running it")
    explain.add_argument("text")
    explain.add_argument("--param", action="append", help="name=value")

    integrate = sub.add_parser("integrate")
    integrate.add_argument("subject")
    integrate.add_argument("--target", default="auto")

    release = sub.add_parser("release")
    release.add_argument("id")
    release.add_argument("--force", action="store_true")

    update = sub.add_parser("update")
    update.add_argument("id")

    status = sub.add_parser("status")
    status.add_argument("request_id", nargs="?")

    results = sub.add_parser("results")
    results.add_argument("request_id")

    sub.add_parser("validate")
    sub.add_parser("providers", help="show which providers can serve each core")

    context = sub.add_parser("context", help="measure what a server's tool list would cost an AI")
    context.add_argument("server", nargs=argparse.REMAINDER, help="-- command for an MCP server")

    plugin = sub.add_parser("plug", help="wire up an MCP server from its tool list")
    plugin.add_argument("--replace", action="store_true", help="wire it up again, replacing what was written")
    plugin.add_argument("--rename", action="append", help='tool="Plain name", to choose a name yourself')
    plugin.add_argument("name")
    plugin.add_argument("server", nargs=argparse.REMAINDER, help="-- command for the MCP server")
    unplug = sub.add_parser("unplug", help="remove a plugged server")
    unplug.add_argument("name")
    sub.add_parser("plugged", help="list plugged servers and what each gives")

    scaffold = sub.add_parser("scaffold-provider", help="draft a definition from a live MCP server")
    scaffold.add_argument("name")
    scaffold.add_argument("server", nargs=argparse.REMAINDER, help="-- command for the MCP server")

    args = parser.parse_args(argv)
    if args.command == "hook":
        return _hook(args)
    if args.command == "listen":
        return _listen(args)
    if args.command == "guard":
        return _guard(args)
    if args.command == "operator-key":
        if not operator.at_terminal():
            return 1  # a key is made by a person, never by a script that already holds one
        key = operator.make_key()
        print(f"Operator key saved to {operator.key_file()}\nPass it for one command: DCAP_OPERATOR={key} dcap status")
        return 0
    try:
        if args.command == "validate":
            reg = Registries(_definitions(args), args.simulate)
            print(json.dumps({
                "classes": len(reg.classes), "cores": len(reg.cores), "goals": len(reg.goals),
                "bridges": len(reg.bridges),
                "providers": len({i.provider for v in reg.implementations.values() for i in v}),
                "mode": "simulated" if reg.simulate else "live",
            }))
            return 0
        if args.command == "context":
            print(json.dumps(_context(args), indent=2))
            return 0
        if args.command == "plug":
            command = _command(args.server)
            if not command:
                parser.error("give the server command after --")
            result = plug.plug(args.name, command, args.definitions, args.replace, _renames(args.rename), args.simulate)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "unplug":
            print(json.dumps(plug.unplug(args.name), indent=2))
            return 0
        if args.command == "plugged":
            print(json.dumps(plug.plugged(args.definitions, args.simulate), indent=2))
            return 0
        if args.command == "scaffold-provider":
            reg = Registries(_definitions(args), args.simulate)
            command = _command(args.server)
            if not command:
                parser.error("give the server command after --")
            print(render(draft(args.name, command, reg, Resolver(reg).lex)), end="")
            return 0

        dcap = Dcap(args.workspace, _definitions(args), grants=_grants(args.grant), simulate=args.simulate)
        if args.command == "request":
            result = dcap.request(args.text, _params(args.param), args.target, not args.no_execute)
        elif args.command == "explain":
            result = dcap.explain(args.text, _params(args.param))
        elif args.command == "integrate":
            result = dcap.integrate(args.subject, args.target)
        elif args.command == "release":
            result = dcap.release(args.id, args.force)
        elif args.command == "update":
            result = dcap.update(args.id)
        elif args.command == "providers":
            result = dcap.providers()
        elif args.command == "results":
            result = dcap.results(args.request_id)
        else:
            result = dcap.status(args.request_id)
    except (DcapError, RemoteError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
