"""The AI calls door(). Everything else here is for the operator."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import uuid

from . import bridges as bridge_ops
from .engine import Engine, required_permissions
from .errors import ConflictError, StateError, ValidationFailure
from .facade import (
    NotAssemblable, PACKAGE, assemble, assemble_tools, facade_name, injectable, signature, snake, switch_name,
)
from .observer import Observer
from .planner import Planner
from .registry import Registries
from .resolver import Resolver
from .runtime.policy import load_grants, normalize
from .workspace import Workspace

_DELIVERED = {"integrated", "existing"}
_UNUSABLE = (ConflictError, LookupError, SyntaxError, ValueError, OSError, RecursionError, MemoryError)
_SCRIPT = re.compile(r"\A#!|^if\s+__name__\s*==\s*['\"]__main__['\"]", re.MULTILINE)


def _abstain(reason, detail="", candidates=()):
    out = {"status": "abstained", "reason": reason, "detail": detail}
    if candidates:
        out["candidates"] = list(candidates)
    return out


class Dcap:
    def __init__(self, workspace=".", extra_definitions=(), grants=None, observe=False, simulate=None):
        self.root = Path(workspace).resolve()
        if not self.root.is_dir():
            raise StateError(f"workspace {self.root} is not an existing directory")
        self.registries = Registries(extra_definitions, simulate)
        self.resolver = Resolver(self.registries)
        self.planner = Planner(self.registries)
        self.observer = Observer(self.root)
        self.workspace = Workspace(self.root)
        self._grants = grants
        self.observer.refresh()
        if observe:
            self.observer.start()

    @property
    def grants(self):
        # Read on each use so an operator's change applies without a restart.
        return load_grants() if self._grants is None else normalize(self._grants)

    def _engine(self):
        return Engine(self.registries, self.grants)

    # ---- request ----------------------------------------------------------
    def request(self, text, params=None, target=None, execute=True, require_params=True):
        """Operator view: resolve, integrate, optionally run a goal, and report everything.

        A vague request integrates capability switches and a clear goal integrates its one
        switch, all off. target optionally names code to connect, such as "path.py:Class.method".
        """
        resolution = self.resolver.resolve(text, params, require_params)
        if resolution.mode == "unresolved":
            out = {"mode": "unresolved", "status": "nothing_available", "reason": resolution.reason}
        elif resolution.mode == "direct":
            out = self._direct(resolution, text)
        else:
            out = self._goal(resolution, text, target, execute)
        return self._remember(out)

    def _goal(self, resolution, text, target, execute):
        goal = self.registries.goals[resolution.goal]
        plan = self.planner.for_goal(goal.id)
        out = {"mode": "goal", "goal": goal.name, "goal_id": goal.id, "version": goal.version}
        out["integration"] = self._deliver_goal(plan, goal, text, target)
        if out["integration"]["status"] not in _DELIVERED:
            # Mode B integrates and then executes, so the sequence stops where it failed.
            out["status"] = out["integration"]["status"]
        elif execute:
            out.update(self._engine().run(plan, resolution.params))
        else:
            out["status"] = "not_executed"
        return out

    def _direct(self, resolution, text):
        capabilities, integrations = {}, []
        for class_name in resolution.classes:
            core_ids = self.resolver.expose(class_name, resolution.tokens)
            integration = self._deliver_tools(class_name, core_ids, text)
            integrations.append(integration)
            if integration["status"] in _DELIVERED:
                capabilities[class_name] = list(integration["usage"].values())
        out = {
            "mode": "direct",
            "status": "exposed" if capabilities else integrations[0]["status"],
            "reason": resolution.reason,
            "capabilities": capabilities,
            "integrations": integrations,
        }
        if resolution.suggestion:
            out["suggestion"] = resolution.suggestion
        return out

    def _remember(self, out):
        request_id = "r" + uuid.uuid4().hex[:12]
        stored = {"request_id": request_id, "created": _now(), **out}
        try:
            self.workspace.save_result(request_id, stored)
        except OSError:
            # The workspace may already have changed, so the caller still needs this outcome.
            stored["warnings"] = [{"code": "RESULT_NOT_STORED", "message": "status and results can't find this request"}]
        return stored

    # ---- status and results -----------------------------------------------
    def results(self, request_id):
        """The full stored outcome of an earlier request."""
        stored = self.workspace.load_result(request_id)
        return stored or {"status": "unknown_request", "request_id": request_id}

    def status(self, request_id=None):
        """A request's state, or the project's integrations and anything that has drifted."""
        if request_id:
            stored = self.workspace.load_result(request_id)
            if not stored:
                return {"status": "unknown_request", "request_id": request_id}
            return {key: stored[key] for key in ("request_id", "mode", "status", "created") if key in stored}
        self.observer.refresh()
        symbols = {(s.path, s.qualname): s for s in self.observer.symbols()}
        integrations = {}
        for integration_id, entry in self.workspace.manifest()["integrations"].items():
            edit = entry["edit"]
            problems = self.workspace.conflicts(integration_id)
            if edit and edit["symbol"]:
                symbol = symbols.get((edit["path"], edit["symbol"]))
                if self.observer.unparsed(edit["path"]):
                    problems.append(f"{edit['path']} does not parse, so {edit['symbol']} can't be checked")
                elif symbol is None:
                    problems.append(f"{edit['symbol']} no longer exists")
                elif symbol.signature != edit["signature"]:
                    problems.append(f"the signature of {edit['symbol']} changed")
            integrations[integration_id] = {
                "subject": entry["subject"],
                "entry_point": _entry_point(entry),
                "problems": problems,
                "outdated": not self._is_current(entry),
            }
        out = {"project": self.observer.snapshot(), "integrations": integrations}
        recovered = self.workspace.recovered()
        if recovered:
            out["recovered"] = recovered
        return out

    def _current_version(self, entry):
        if entry.get("cores"):
            return _tools_version(entry["cores"], self.registries)
        definition = self.registries.goals.get(entry["subject"])
        if definition:
            return definition.version
        return str(getattr(self.registries.cores.get(entry["subject"]), "contract", ""))

    def _dependencies(self, subject):
        """The versions a goal was built from: its child goals and its cores."""
        goals, cores = {}, {}

        def walk(goal_id):
            for dep in self.registries.goals[goal_id].dependencies:
                if "core" in dep:
                    cores[dep["core"]] = self.registries.cores[dep["core"]].contract
                else:
                    goals[dep["goal"]] = self.registries.goals[dep["goal"]].version
                    walk(dep["goal"])

        if subject not in self.registries.goals:
            return None
        walk(subject)
        return {"goals": goals, "cores": cores}

    def _is_current(self, entry):
        """Whether the definitions and the provider selection still match what was integrated."""
        if self._current_version(entry) != entry["version"]:
            return False
        stored = entry.get("dependencies")
        if stored is not None and stored != self._dependencies(entry["subject"]):
            return False
        for core_id, provider in entry.get("providers", {}).items():
            options = self.registries.providers_for(core_id)
            if not options or f"{options[0].provider}@{options[0].version}" != provider:
                return False
        return True

    # ---- delivery ---------------------------------------------------------
    def _existing(self, subject):
        for integration_id, entry in self.workspace.manifest()["integrations"].items():
            if entry["subject"] == subject:
                return integration_id, entry
        return None, None

    def _deliver_again(self, integration_id, entry):
        """A repeat request changes nothing, but it can't report code that is no longer there."""
        problems = self.workspace.conflicts(integration_id)
        if problems:
            return {
                "status": "conflict", "id": integration_id, "entry_point": _entry_point(entry),
                "detail": "; ".join(problems),
            }
        if self._is_current(entry):
            return _delivered("existing", integration_id, entry)
        return self.update(integration_id)

    def _deliver_goal(self, plan, goal, text, target):
        integration_id, entry = self._existing(plan.subject)
        if entry and (not target or _is_entry_point(entry, target)):
            return self._deliver_again(integration_id, entry)
        return self._integrate_plan(plan, text, target or "auto", goal, fallback=True)

    def _deliver_tools(self, class_name, core_ids, text, keep=None):
        cls = self.registries.classes[class_name]
        whole = list(core_ids) == list(cls.cores)
        digest = hashlib.sha256(",".join(sorted(core_ids)).encode()).hexdigest()[:8]
        file_name = class_name if whole else f"{class_name}_{digest}"
        subject = f"tools.{file_name}"
        version = _tools_version(core_ids, self.registries)
        integration_id, entry = self._existing(subject)
        if entry:
            return self._deliver_again(integration_id, entry)

        self.observer.refresh()
        blocked = self._unsupported_language()
        if blocked:
            return blocked
        served = sorted(c for c in core_ids if injectable(c, self.registries))
        unserved = [self.registries.cores[c].name for c in core_ids if c not in served]
        if not served:
            return _abstain("no_injectable_provider", ", ".join(unserved))
        names = [self.registries.cores[c].name for c in served]
        plans = {self.registries.cores[c].name: self.planner.for_core(c) for c in served}
        try:
            files, providers = assemble_tools(plans, self.registries)
        except NotAssemblable as exc:
            return _abstain("no_injectable_provider", str(exc))
        bridge = self.registries.bridge_of_kind("toggles")
        kept = (keep or {}).get("switches", {})
        kept_names = (keep or {}).get("names", {})
        usage = {}

        def body(_variable, source):
            clause, os_name = bridge_ops.os_import(source)
            wanted = [kept_names.get(n, snake(n)) for n in names]
            tools = []
            usage.clear()
            for n, local in zip(names, bridge_ops.free_names(source, wanted, {os_name})):
                usage[local] = n
                module = f"{PACKAGE}.{facade_name(plans[n].subject, self.registries)}"
                tools.append(f"from {module} import run as {local}")
                tools.append(bridge_ops.switch_line(
                    os_name, switch_name(n), kept.get(switch_name(n), "false"), f"{local}({signature(plans[n])})",
                ))
            return bridge_ops.fill(bridge, os=clause, tools="\n".join(tools))

        record = {
            "subject": subject, "version": version, "request": text, "cores": list(core_ids),
            "class": class_name, "providers": providers, "bridge": f"{bridge.id}@{bridge.version}",
            "module": PACKAGE, "created": _now(), "usage": usage,
        }
        if unserved:
            record["unavailable"] = unserved
        return self._commit_block(subject, version, None, body, files, record, keep)

    def _unsupported_language(self):
        languages = self.observer.snapshot()["languages"]
        if languages and "python" not in languages:
            return _abstain("no_compatible_bridge", f"No bridge exists yet for {', '.join(sorted(languages))}.")
        return None

    def _commit_block(self, subject, version, variable, body, files, record, keep=None):
        """Place a predefined block in the most recently changed Python file.

        keep carries what an update must preserve: the same file, variable and switches.
        """
        paths = [keep["path"]] if keep else self.observer.recent_python()
        wanted = (keep or {}).get("variable") or variable
        failure = None
        for path in paths:
            try:
                before = self.workspace.read(path)
                name = bridge_ops.free_name(before, wanted) if wanted else None
                root, beside = self._placement(path)
                lines = [f"# {path}", *body(name, before)]
                if beside:
                    lines = bridge_ops.dual_import(lines, PACKAGE)
                identity = f"{subject}|{version}|{path}|block"
                integration_id = _identify(identity)
                after = bridge_ops.insert_block(before, lines, integration_id)
            except _UNUSABLE as exc:
                failure = exc
                continue  # mid-edit or unreadable: try the next file
            placed = {_under(root, rel): text for rel, text in files.items()}
            edit = {
                "path": path, "symbol": None, "source_before": before, "source_after": after,
                "fragment": [], "signature": None,
            }
            return self._commit(integration_id, {**record, "variable": name}, placed, edit)
        if keep:
            return _abstain(
                "workspace_conflict",
                f"The old integration was released, but the new version could not land in {keep['path']}: {failure}",
            )
        # An empty project has no working code yet, so the package is all there is.
        integration_id = _identify(f"{subject}|{version}|package")
        return self._commit(integration_id, {**record, "snippet": body(wanted, "")}, files, None)

    def _commit(self, integration_id, record, files, edit):
        try:
            stored = self.workspace.integrate(integration_id, record, files, edit)
        except (ConflictError, ValidationFailure) as exc:
            return _abstain("workspace_conflict", str(exc))
        except StateError as exc:
            return _abstain("workspace_write_failed", str(exc))
        except OSError as exc:
            return _abstain("workspace_write_failed", f"Nothing was changed: {exc}")
        self.observer.refresh()
        return _delivered("integrated", integration_id, stored)

    # ---- explicit integration ---------------------------------------------
    def _subject(self, subject):
        """Find a goal by id or name, or a capability by name or core id. Returns (plan, goal)."""
        goal = self.registries.goals.get(subject) or next(
            (g for g in self.registries.goals.values() if g.name == subject), None
        )
        if goal:
            return self.planner.for_goal(goal.id), goal
        core = self.registries.cores.get(subject) or self.registries.core_by_name(subject)
        return (self.planner.for_core(core.id), None) if core else (None, None)

    def integrate(self, subject, target="auto", keep=None):
        """Connect a goal or capability to one existing unimplemented symbol, or abstain."""
        plan, goal = self._subject(subject)
        if plan is None:
            return _abstain("unknown_subject", subject)
        return self._integrate_plan(plan, subject, target, goal, keep=keep)

    def _switch_for(self, plan, goal):
        return switch_name(goal.name if goal else self.registries.cores[plan.subject].name)

    def _vocabulary(self, plan, goal):
        lex = self.resolver.lex
        classes = {self.registries.cores[s.core].cls for s in plan.steps}
        words = [kw for c in classes for kw in self.registries.classes[c].keywords]
        words += [goal.name, *goal.aliases] if goal else [self.registries.cores[plan.subject].name]
        return lex.tokens(" ".join(words))

    def _integrate_plan(self, plan, request, target, goal, fallback=False, keep=None):
        self.observer.refresh()
        blocked = self._unsupported_language()
        if blocked:
            return blocked
        symbols = self.observer.symbols()
        if target == "auto":
            match = bridge_ops.select_target(
                symbols, plan.params, self._vocabulary(plan, goal), self.registries, self.resolver.lex
            )
            if isinstance(match, bridge_ops.Abstain):
                if fallback:
                    return self._integrate_goal_block(plan, request, goal)
                return _abstain(match.reason, match.detail, match.candidates)
        else:
            found = bridge_ops.find_symbol(target, symbols)
            if not found:
                return _abstain("symbol_not_found", target)
            if len(found) > 1:
                return _abstain("ambiguous_target", target, [f"{s.path}:{s.qualname}" for s in found])
            if not found[0].is_stub:
                return _abstain("target_has_user_code", "DCAP will not overwrite an implemented symbol.")
            if not found[0].fillable:
                return _abstain(
                    "target_is_declaration", "Abstract, overload, Protocol and special methods are not filled in."
                )
            match = bridge_ops.match_bridge(found[0], plan.params, self.registries)
            if match is None:
                return _abstain("no_compatible_bridge", f"No bridge fits {found[0].qualname}.")
        switch = self._switch_for(plan, goal)
        try:
            files, providers = assemble(plan, self.registries, switch)
        except NotAssemblable as exc:
            return _abstain("no_injectable_provider", str(exc))

        symbol = match.symbol
        try:
            import_root, beside = self._placement(symbol.path)
        except _UNUSABLE as exc:
            return _abstain("workspace_conflict", str(exc))
        files = {_under(import_root, rel): text for rel, text in files.items()}
        integration_id = _identify(f"{plan.subject}|{plan.version}|{symbol.path}|{symbol.qualname}")
        module = f"{PACKAGE}.{facade_name(plan.subject, self.registries)}"
        try:
            before = self.workspace.read(symbol.path)
            clause, os_name = bridge_ops.os_import(before)
            value = (keep or {}).get("switches", {}).get(switch, "false")
            line = bridge_ops.switch_line(os_name, switch, value)
            body = [f"# {symbol.path}", *bridge_ops.render(match, module, clause, line)]
            if beside:
                body = bridge_ops.dual_import(body, PACKAGE)
            after, fragment = bridge_ops.splice(before, symbol.qualname, body, integration_id, symbol.lineno)
        except _UNUSABLE as exc:
            return _abstain("workspace_conflict", str(exc))
        record = {
            "subject": plan.subject,
            "version": plan.version,
            "request": request,
            "bridge": f"{match.bridge.id}@{match.bridge.version}",
            "providers": providers,
            "dependencies": self._dependencies(plan.subject),
            "module": module,
            "usage": {"run": signature(plan)},
            "requires_permissions": required_permissions(plan, self.registries),
            "created": _now(),
        }
        edit = {
            "path": symbol.path,
            "symbol": symbol.qualname,
            "source_before": before,
            "source_after": after,
            "fragment": fragment,
            "signature": symbol.signature,
            "lineno": symbol.lineno,
        }
        return self._commit(integration_id, record, files, edit)

    def _integrate_goal_block(self, plan, request, goal, keep=None):
        switch = self._switch_for(plan, goal)
        try:
            files, providers = assemble(plan, self.registries, switch)
        except NotAssemblable as exc:
            return _abstain("no_injectable_provider", str(exc))
        module = f"{PACKAGE}.{facade_name(plan.subject, self.registries)}"
        bridge = self.registries.bridge_of_kind("block")

        def body(variable, source):
            clause, os_name = bridge_ops.os_import(source)
            value = (keep or {}).get("switches", {}).get(switch, "false")
            line = bridge_ops.switch_line(os_name, switch, value, f"{variable}({signature(plan)})")
            return bridge_ops.fill(bridge, module=module, name=variable, os=clause, switches=line)

        record = {
            "subject": plan.subject, "version": plan.version, "request": request, "providers": providers,
            "dependencies": self._dependencies(plan.subject),
            "bridge": f"{bridge.id}@{bridge.version}", "module": module, "created": _now(),
            "usage": {"run": signature(plan)},
            "requires_permissions": required_permissions(plan, self.registries),
        }
        return self._commit_block(plan.subject, plan.version, snake(goal.name), body, files, record, keep)

    def _placement(self, rel):
        """Where the generated package goes for a file, and whether that file imports it both ways.

        A file directly in the project, or in a package imported from its root, shares one import
        root with the package. A script or a loose folder can be run or imported in several ways,
        so the package sits beside it and the import in the file picks the form that works.
        """
        folder = (self.root / rel).parent
        if folder == self.root:
            return "", False
        here = folder.relative_to(self.root).as_posix()
        if not (folder / "__init__.py").exists() or _SCRIPT.search(self.workspace.read(rel)):
            return here, True
        while (folder / "__init__.py").exists() and folder != self.root:
            folder = folder.parent
        return (folder.relative_to(self.root).as_posix() if folder != self.root else ""), False

    # ---- lifecycle --------------------------------------------------------
    def release(self, integration_id, force=False):
        try:
            return {"status": "released", **self.workspace.release(integration_id, force)}
        except (ConflictError, OSError) as exc:
            return {"status": "conflict", "detail": str(exc)}

    def update(self, integration_id):
        """Re-integrate at current definitions. Refuses if anything changed by hand."""
        entry = self.workspace.manifest()["integrations"].get(integration_id)
        if entry is None:
            return {"status": "failed", "detail": "unknown integration"}
        problems = self.workspace.conflicts(integration_id)
        if problems:
            return {"status": "conflict", "detail": "; ".join(problems)}
        edit = entry["edit"]
        reference = _entry_point(entry)
        keep = None
        switches = self.workspace.switch_values(integration_id, edit["path"]) if edit else {}
        if edit and not edit["symbol"]:
            keep = {
                "path": edit["path"], "variable": entry.get("variable"), "switches": switches,
                "names": {name: local for local, name in entry.get("usage", {}).items()},
            }
        if entry.get("cores"):
            cls = self.registries.classes.get(entry["class"])
            if cls is None:
                return _abstain("unknown_subject", f"{entry['class']}; {reference} is unchanged.")
            # An integration of a whole class stays whole as the class grows.
            cores = cls.cores if cls and entry["subject"] == f"tools.{entry['class']}" else entry["cores"]
            missing = [c for c in cores if c not in self.registries.cores]
            if missing:
                return _abstain("unknown_subject", ", ".join(missing))
            if not any(injectable(c, self.registries) for c in cores):
                return _abstain("no_injectable_provider", f"{reference} is unchanged.")
            served = [c for c in cores if injectable(c, self.registries)]
            plans = {self.registries.cores[c].name: self.planner.for_core(c) for c in served}
            try:
                files, _ = assemble_tools(plans, self.registries)
            except NotAssemblable as exc:
                return _abstain("no_injectable_provider", f"{exc}; {reference} is unchanged.")
            blocked = self._held_elsewhere(integration_id, edit, files)
            if blocked:
                return blocked
            released = self.release(integration_id)
            if released["status"] != "released":
                return released
            self.observer.refresh()
            return self._deliver_tools(entry["class"], cores, entry["request"], keep)
        plan, goal = self._subject(entry["subject"])
        if plan is None:
            return _abstain("unknown_subject", entry["subject"])
        # Check the new version can land before touching the working integration.
        self.observer.refresh()
        try:
            files, _ = assemble(plan, self.registries, self._switch_for(plan, goal))
        except NotAssemblable as exc:
            return _abstain("no_injectable_provider", f"{exc}; {reference} is unchanged.")
        blocked = self._held_elsewhere(integration_id, edit, files)
        if blocked:
            return blocked
        if edit and edit["symbol"]:
            found = bridge_ops.find_symbol(reference, self.observer.symbols())
            if len(found) != 1 or bridge_ops.match_bridge(found[0], plan.params, self.registries) is None:
                return _abstain("no_compatible_bridge", f"{reference} is unchanged.")
        released = self.release(integration_id)
        if released["status"] != "released":
            return released
        self.observer.refresh()
        if edit and edit["symbol"]:
            return self.integrate(entry["subject"], reference, {"switches": switches})
        return self._integrate_goal_block(plan, entry["request"], goal, keep)

    def _held_elsewhere(self, integration_id, edit, files):
        """Refuse an update another integration would stop from landing, before anything is released."""
        root = self._placement(edit["path"])[0] if edit else ""
        placed = {_under(root, rel): text for rel, text in files.items()}
        held = self.workspace.held_by_others(integration_id, placed)
        if not held:
            return None
        return _abstain(
            "shared_file", f"Nothing changed. Another integration uses {', '.join(held)} at the old version; release it first.",
        )

    # ---- the door ---------------------------------------------------------
    def door(self, text):
        """What happens after the AI says yes. Returns only what was pasted into its code.

        Nothing runs here. The AI turns on a switch in the pasted code when it wants to use it.
        """
        if not isinstance(text, str):
            raise TypeError("the request must be text")
        out = self.request(text, execute=False, require_params=False)
        items = out.get("integrations") or [out.get("integration") or {}]
        blocks = [self.workspace.block_text(i["id"]) for i in items if i.get("status") in _DELIVERED]
        return "\n".join(block for block in blocks if block)

    # ---- operator views ---------------------------------------------------
    def explain(self, text, params=None):
        """Operator view of how a request resolves. Runs nothing and writes nothing."""
        resolution = self.resolver.resolve(text, params)
        out = {
            "mode": resolution.mode,
            "reason": resolution.reason,
            "tokens": sorted(resolution.tokens),
            "candidates": [{"goal": g, "score": round(s, 3)} for s, g in resolution.ranked[:3]],
            "classes": resolution.classes,
        }
        if resolution.suggestion:
            out["suggestion"] = resolution.suggestion
        if resolution.mode == "goal":
            plan = self.planner.for_goal(resolution.goal)
            out["goal"] = resolution.goal
            out["params"] = resolution.params
            out["stages"] = [
                [{
                    "stage": step.id,
                    "capability": self.registries.cores[step.core].name,
                    "uses": {name: ref[1] for name, ref in step.inputs.items()},
                    "providers": [i.provider for i in self.registries.providers_for(step.core)],
                } for step in level]
                for level in plan.levels()
            ]
            denied = self._engine().preflight(plan, resolution.params)
            out["authorization"] = denied or {"status": "granted"}
        return out

    def providers(self):
        """Operator view of provider coverage for every core."""
        coverage = {}
        for core in self.registries.cores.values():
            coverage[core.id] = [
                {"provider": i.provider, "kind": i.kind, "priority": i.priority, "available": i.available()}
                for i in sorted(self.registries.implementations.get(core.id, []),
                                key=lambda item: (item.priority, item.provider))
            ]
        return coverage


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identify(identity):
    return "i" + hashlib.sha256(identity.encode()).hexdigest()[:10]


def _under(root, rel):
    return f"{root}/{rel}" if root else rel


def _tools_version(core_ids, registries):
    """The contracts of the capabilities that can be delivered now."""
    return ",".join(
        str(registries.cores[c].contract)
        for c in sorted(core_ids)
        if c in registries.cores and injectable(c, registries)
    )


def _entry_point(entry):
    edit = entry["edit"]
    if not edit:
        return entry["module"]
    if edit["symbol"]:
        return f"{edit['path']}:{edit['symbol']}"
    return f"{edit['path']}:{entry['variable']}" if entry.get("variable") else edit["path"]


def _is_entry_point(entry, target):
    edit = entry["edit"]
    return bool(edit and edit["symbol"]) and target in {edit["symbol"], _entry_point(entry)}


def _delivered(status, integration_id, entry):
    out = {
        "status": status,
        "id": integration_id,
        "entry_point": _entry_point(entry),
        "module": entry["module"],
        "usage": entry.get("usage", {}),
    }
    if entry.get("requires_permissions"):
        out["requires_permissions"] = entry["requires_permissions"]
    if entry.get("unavailable"):
        out["unavailable"] = entry["unavailable"]
    return out
