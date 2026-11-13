"""Assemble local code: a goal interface, or a capability interface with switches."""
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
import pprint

from .engine import DEFAULT_RETRIES, DEFAULT_TIMEOUT, remote_spec
from .registry import module_name, snake
from . import runtime

PACKAGE = "dcap_capabilities"


class NotAssemblable(Exception):
    pass


def facade_name(subject, registries):
    name = module_name(subject)
    if subject in registries.cores and any(module_name(g) == name for g in registries.goals):
        return f"{name}_core"  # a goal and a core can share a name but never a file
    return name


def _vendor_sources(impl, core_name):
    """Where each file a native provider copies comes from: {file name: path}."""
    source = impl.operation.partition(":")[0]
    runtime_dir = Path(runtime.__file__).parent
    spec = find_spec(source)
    folders = [Path(spec.origin).parent] if spec and spec.origin else []
    sources = {}
    for name in [*impl.vendor]:
        path = next((f / name for f in [*folders, runtime_dir] if (f / name).is_file()), None)
        if path is None:
            raise NotAssemblable(core_name)
        sources[name] = path
    if f"{source.rsplit('.', 1)[-1]}.py" not in sources:
        raise NotAssemblable(core_name)
    return sources


def select_injectable(core_id, registries):
    """The provider the engine would choose first, so the run and the integrated code agree."""
    options = registries.providers_for(core_id)
    if not options:
        raise NotAssemblable(registries.cores[core_id].name)
    impl = options[0]
    if impl.kind == "native" and impl.vendor:
        _vendor_sources(impl, registries.cores[core_id].name)
    return impl


def injectable(core_id, registries):
    try:
        select_injectable(core_id, registries)
    except NotAssemblable:
        return False
    return True


@dataclass
class _Module:
    imports: set = field(default_factory=set)
    vendored: set = field(default_factory=set)
    sources: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    remote_specs: list = field(default_factory=list)
    types: dict = field(default_factory=dict)


def _collect_type(name, registries, module):
    def walk(schema):
        if "ref" in schema:
            _collect_type(schema["ref"], registries, module)
        if "items" in schema:
            walk(schema["items"])
        for sub in schema.get("properties", {}).values():
            walk(sub)

    if name not in module.types:
        module.types[name] = registries.types[name]
        walk(module.types[name])


def _emit(plan, registries, module, indent):
    """Render one plan as the deterministic statements of a function body."""
    body, guards = [], set()
    for step in plan.steps:
        core = registries.cores[step.core]
        impl = select_injectable(step.core, registries)
        module.providers[step.core] = f"{impl.provider}@{impl.version}"
        args = {}
        for name, ref in step.inputs.items():
            args[name] = pprint.pformat(ref[1], width=10**6) if ref[0] == "literal" else ref[1]
        scope = args.get(core.scope) if core.scope else None
        for permission in core.permissions:
            if (permission, scope) in guards:
                continue
            guards.add((permission, scope))
            module.imports.add("from .policy import authorize")
            module.vendored.add("policy.py")
            body.append(f"authorize({permission!r}, {scope})" if scope else f"authorize({permission!r})")
        if impl.kind == "native":
            source, _, attr = impl.operation.partition(":")
            local = source.rsplit(".", 1)[-1]
            alias = f"_{local}_{attr}"
            if impl.vendor:
                module.imports.add(f"from .{local} import {attr} as {alias}")
                module.vendored.update(impl.vendor)
                module.sources.update(_vendor_sources(impl, core.name))
            else:
                # Nothing to copy: it comes from its installed package, and only when it is called.
                body.append(f"from {source} import {attr} as {alias}")
            call = f"{alias}({', '.join(f'{k}={v}' for k, v in args.items())})"
        else:
            module.imports.add("from .remote import call_remote")
            module.vendored.add("remote.py")
            constant = f"_REMOTE_{len(module.remote_specs)}"
            module.remote_specs.append(f"{constant} = {pprint.pformat(remote_spec(impl), sort_dicts=True)}")
            mapping = ", ".join(f"{k!r}: {v}" for k, v in args.items())
            timeout = core.execution.get("timeout_s", DEFAULT_TIMEOUT)
            call = f"call_remote({constant}, {{{mapping}}}, {timeout})"
        module.imports.add("from .contract import checked_call")
        module.vendored.add("contract.py")
        _collect_type(core.output, registries, module)
        for name in args:
            _collect_type(core.inputs[name]["type"], registries, module)
        inputs = ", ".join(f"{k!r}: ({core.inputs[k]['type']!r}, {v})" for k, v in args.items())
        retries = core.execution.get("retries", DEFAULT_RETRIES)
        body.append(
            f"{step.id} = checked_call({core.name!r}, _TYPES, {{{inputs}}}, {core.output!r}, lambda: {call}, {retries})"
        )
    body.append(f"return {plan.result}")
    return [f"{indent}{line}" for line in body]


def signature(plan):
    ordered = sorted(plan.params.items(), key=lambda item: not item[1]["required"])
    return ", ".join(n if s["required"] else f"{n}=None" for n, s in ordered)


def _files(subject_file, docstring, module, code):
    runtime_dir = Path(runtime.__file__).parent
    types = f"_TYPES = {pprint.pformat(module.types, sort_dicts=True)}"
    source = [f'"""{docstring}"""', *sorted(module.imports), "", types, *module.remote_specs, "", *code, ""]
    files = {
        f"{PACKAGE}/__init__.py": '"""Capabilities integrated by DCAP. Do not edit by hand."""\n',
        f"{PACKAGE}/{subject_file}.py": "\n".join(source),
    }
    for name in sorted(module.vendored):
        path = module.sources.get(name, runtime_dir / name)
        files[f"{PACKAGE}/{name}"] = path.read_text(encoding="utf-8")
    return files


def switch_name(name):
    return snake(name).upper()


_REQUIRE = [
    "def _require(name):",
    '    if os.environ.get(name, "false").strip().lower() != "true":',
    """        raise RuntimeError(f'{name} is off. Set "{name}" to "true".')""",
    "",
]


def assemble(plan, registries, switch):
    """A single `run` for one plan, off until its switch is "true". Returns (files, providers)."""
    module = _Module()
    module.imports.add("import os")
    code = [
        *_REQUIRE,
        f"def run({signature(plan)}):",
        f"    _require({switch!r})",
        *_emit(plan, registries, module, "    "),
    ]
    doc = f"Generated by DCAP for {plan.subject} v{plan.version}. Managed by .dcap/manifest.json."
    return _files(facade_name(plan.subject, registries), doc, module, code), module.providers


def assemble_tools(plans, registries):
    """One module per capability, each off until its own switch is "true".

    `plans` maps capability names to plans. Returns (files, providers).
    """
    files, providers = {}, {}
    for name, plan in plans.items():
        made, used = assemble(plan, registries, switch_name(name))
        files.update(made)
        providers.update(used)
    return files, providers
