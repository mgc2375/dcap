"""The four independent registries, plus load-time cross-validation."""
import ast
from dataclasses import dataclass, field
from importlib.util import find_spec
import keyword
import os
from pathlib import Path
import re
import shutil

import yaml

from .errors import RegistryError

BUILTIN = Path(__file__).parent / "definitions"
_RUNTIME_MODULES = {"__init__", "contract", "policy", "remote", "placeholder", "live"}
_BRIDGE_PLACEHOLDERS = {
    "function": {"module", "args", "os", "switches"},
    "method": {"module", "args", "os", "switches"},
    "block": {"module", "name", "os", "switches"},
    "toggles": {"os", "tools"},
}
_BRIDGE_SAMPLES = {
    "module": "dcap_sample.facade", "package": "dcap_sample", "file": "facade", "args": "target=target",
    "name": "sample", "os": "os", "switches": 'os.environ["SAMPLE"] = "false"',
    "tools": 'from dcap_sample.facade import run as sample\nos.environ["SAMPLE"] = "false"',
}


@dataclass
class Core:
    id: str
    cls: str
    name: str
    contract: int
    description: str
    inputs: dict
    output: str
    permissions: list
    execution: dict
    scope: str | None = None


@dataclass
class CapabilityClass:
    name: str
    title: str
    description: str
    keywords: list
    expose_all_below: int
    cores: list


@dataclass
class Goal:
    id: str
    version: str
    name: str
    outcome: str
    aliases: list
    parameters: dict
    classes: list
    dependencies: list
    result: str


@dataclass
class Implementation:
    provider: str
    version: str
    kind: str
    priority: int
    operation: str
    vendor: list
    command: list = field(default_factory=list)
    input_map: dict = field(default_factory=dict)
    output_key: str | None = None
    requires: list = field(default_factory=list)
    url: str = ""
    method: str = "POST"
    headers_env: dict = field(default_factory=dict)
    before: list = field(default_factory=list)
    text_output: bool = False

    def available(self):
        if self.kind == "native":
            return all(find_spec(m) for m in [self.operation.split(":")[0], *self.requires])
        if self.kind == "mcp":
            return bool(self.command) and shutil.which(self.command[0]) is not None
        if self.kind == "http":
            return self.url.startswith(("https://", "http://"))
        return False


@dataclass
class Bridge:
    id: str
    version: str
    language: str
    kind: str
    is_async: bool
    score: float
    template: str


def snake(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def module_name(identifier):
    """The module a core or goal gets in the generated package."""
    return identifier.lower().replace(".", "_")


def _need(data, key, where):
    if key not in data:
        raise RegistryError(f"{where}: missing '{key}'")
    return data[key]


def _need_identifier(name, where):
    if not name.isidentifier() or keyword.iskeyword(name):
        raise RegistryError(f"{where}: '{name}' must be a valid identifier")


def _load_yaml(path):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: expected a mapping at the top level")
    return data


def _local_command(command, where):
    """A command part starting with ./ names a file beside the definition, wherever it is run from."""
    folder = Path(where).parent
    return [str((folder / part).resolve()) if isinstance(part, str) and part.startswith("./") else part for part in command]


def _check_bridge(bridge, where):
    """A bridge must be able to deliver a capability, so one that can't never loads."""
    required = _BRIDGE_PLACEHOLDERS.get(bridge.kind)
    if required is None:
        raise RegistryError(f"{where}: bridge {bridge.id} has unknown kind '{bridge.kind}'")
    if not isinstance(bridge.score, (int, float)) or isinstance(bridge.score, bool):
        raise RegistryError(f"{where}: bridge {bridge.id} needs a numeric score")
    if not isinstance(bridge.template, str):
        raise RegistryError(f"{where}: bridge {bridge.id} needs a text template")
    used = set(re.findall(r"\{\{(.*?)\}\}", bridge.template))
    if used != required:
        raise RegistryError(f"{where}: bridge {bridge.id} must use exactly the placeholders {sorted(required)}")
    text = bridge.template
    for key, value in _BRIDGE_SAMPLES.items():
        text = text.replace("{{" + key + "}}", value)
    indented = "".join(f"    {line}\n" for line in text.strip().splitlines())
    try:
        tree = ast.parse(f"async def _sample(target):\n{indented}")
    except SyntaxError as exc:
        raise RegistryError(f"{where}: bridge {bridge.id} does not parse: {exc}") from exc
    nodes = list(ast.walk(tree))
    if not any(isinstance(n, ast.ImportFrom) and n.module in {_BRIDGE_SAMPLES["module"], _BRIDGE_SAMPLES["package"]} for n in nodes):
        raise RegistryError(f"{where}: bridge {bridge.id} never imports the generated module")
    if bridge.kind in {"function", "method"}:
        returned = [m for n in nodes if isinstance(n, ast.Return) and n.value for m in ast.walk(n.value)]
        if not any(isinstance(m, ast.Name) and m.id == "_dcap_run" for m in returned):
            raise RegistryError(f"{where}: bridge {bridge.id} must return the result of _dcap_run")


def simulating():
    """True when the operator asked for canned data with DCAP_SIMULATE."""
    return os.environ.get("DCAP_SIMULATE", "").strip().lower() in {"1", "true", "yes", "on"}


class Registries:
    """Capability, goal, provider and bridge registries.

    A provider can declare `simulated: true` or `simulated: false`. Only the ones matching
    this run load, so canned data never stands in for a live provider that failed.
    """

    def __init__(self, extra_dirs=(), simulate=None):
        self.simulate = simulating() if simulate is None else bool(simulate)
        self.types = {}
        self.classes = {}
        self.cores = {}
        self.goals = {}
        self.implementations = {}
        self.bridges = []
        self.vocabulary = {}
        for root in [BUILTIN, *map(Path, extra_dirs)]:
            self._load_dir(root)
        try:
            self._validate()
        except (AttributeError, KeyError, TypeError) as exc:
            raise RegistryError(f"malformed definition: {exc!r}") from exc

    def _load_dir(self, root):
        vocab = root / "vocabulary.yaml"
        if vocab.exists():
            for key, value in _load_yaml(vocab).items():
                # YAML reads bare words such as on and no as booleans.
                words = [w for group in value.values() for w in group] if isinstance(value, dict) else value
                if not all(isinstance(word, str) for word in words):
                    raise RegistryError(f"{vocab}: every word in '{key}' must be a string; quote it")
                if isinstance(value, dict):
                    self.vocabulary.setdefault(key, {}).update(value)
                else:
                    self.vocabulary.setdefault(key, []).extend(value)
        loaders = [
            ("capabilities", self._add_class),
            ("goals", self._add_goals),
            ("providers", self._add_providers),
            ("bridges", self._add_bridges),
        ]
        for folder, loader in loaders:
            for path in sorted((root / folder).glob("*.yaml")):
                try:
                    loader(_load_yaml(path), str(path))
                except (AttributeError, KeyError, TypeError) as exc:
                    raise RegistryError(f"{path}: malformed definition: {exc!r}") from exc

    def _add_class(self, data, where):
        name = _need(data, "class", where)
        self.types.update(data.get("types", {}))
        core_ids = []
        for core_id, spec in _need(data, "cores", where).items():
            if core_id in self.cores:
                raise RegistryError(f"{where}: duplicate core {core_id}")
            self.cores[core_id] = Core(
                core_id, name, _need(spec, "name", core_id), _need(spec, "contract", core_id),
                spec.get("description", ""), spec.get("inputs", {}), _need(spec, "output", core_id),
                spec.get("permissions", []), spec.get("execution", {}), spec.get("scope"),
            )
            core_ids.append(core_id)
        self.classes[name] = CapabilityClass(
            name, data.get("title", name), data.get("description", ""), data.get("keywords", []),
            data.get("expose_all_below", 8), core_ids,
        )

    def _add_goals(self, data, where):
        for spec in _need(data, "goals", where):
            goal_id = _need(spec, "goal", where)
            if goal_id in self.goals:
                raise RegistryError(f"{where}: duplicate goal {goal_id}")
            self.goals[goal_id] = Goal(
                goal_id, str(_need(spec, "version", goal_id)), _need(spec, "name", goal_id),
                spec.get("outcome", ""), _need(spec, "aliases", goal_id), spec.get("parameters", {}),
                spec.get("classes", []), _need(spec, "dependencies", goal_id),
                _need(_need(spec, "result", goal_id), "source", goal_id),
            )

    def _add_providers(self, data, where):
        for spec in _need(data, "providers", where):
            provider = _need(spec, "provider", where)
            if spec.get("simulated") not in (None, self.simulate):
                continue
            for core_id, impl in _need(spec, "implements", provider).items():
                core = self.cores.get(core_id)
                if core is None:
                    raise RegistryError(f"{provider}: implements unknown core {core_id}")
                if impl.get("contract") != core.contract:
                    raise RegistryError(f"{provider}: contract mismatch for {core_id}")
                self.implementations.setdefault(core_id, []).append(Implementation(
                    provider, str(spec.get("version", "0")), _need(spec, "kind", provider),
                    spec.get("priority", 100), _need(impl, "operation", core_id), spec.get("vendor", []),
                    _local_command(spec.get("command", []), where), impl.get("input_map", {}), impl.get("output_key"),
                    spec.get("requires", []), spec.get("url", ""), impl.get("method", "POST"),
                    spec.get("headers_env", {}), impl.get("before", []), spec.get("text_output", False),
                ))

    def _add_bridges(self, data, where):
        for spec in _need(data, "bridges", where):
            match = _need(spec, "match", where)
            bridge = Bridge(
                _need(spec, "bridge", where), str(spec.get("version", "0")), _need(spec, "language", where),
                _need(match, "kind", where), bool(match.get("async")), spec.get("score", 1.0),
                _need(spec, "template", where),
            )
            _check_bridge(bridge, where)
            self.bridges.append(bridge)

    def _check_pasteable(self):
        """Every capability gets its own module, import and switch, so none of those can collide."""
        vendored = {Path(name).stem for impls in self.implementations.values() for i in impls for name in i.vendor}
        taken = _RUNTIME_MODULES | vendored
        modules, tools = {}, {}
        for core in self.cores.values():
            module, tool = module_name(core.id), snake(core.name)
            for what, value in (("module", module), ("name", tool)):
                if not value.isidentifier() or keyword.iskeyword(value):
                    raise RegistryError(f"{core.id}: {what} '{value}' is not a valid Python name")
            if module in taken:
                raise RegistryError(f"{core.id}: module '{module}' is reserved")
            for seen, value in ((modules, module), (tools, tool)):
                if value in seen:
                    raise RegistryError(f"{core.id} and {seen[value]} both become '{value}'")
                seen[value] = core.id
        for goal in self.goals.values():
            module = module_name(goal.id)
            if module in taken or not module.isidentifier() or keyword.iskeyword(module):
                raise RegistryError(f"{goal.id}: module '{module}' can't be used")

    def _validate(self):
        self._check_pasteable()
        names = {}
        for core in self.cores.values():
            if core.name in names:
                raise RegistryError(f"capability name '{core.name}' used by {names[core.name]} and {core.id}")
            names[core.name] = core.id
            for spec in core.inputs.values():
                self._check_type(spec["type"], core.id)
            self._check_type(core.output, core.id)
            if core.scope and core.scope not in core.inputs:
                raise RegistryError(f"{core.id}: scope '{core.scope}' is not an input")
        for goal in self.goals.values():
            self.goal_output_type(goal.id, ())

    def _check_type(self, name, where):
        if name not in self.types:
            raise RegistryError(f"{where}: unknown type {name}")

    def goal_output_type(self, goal_id, stack):
        """Type-check a goal's wiring and return its result type."""
        if goal_id in stack:
            raise RegistryError(f"goal cycle: {' -> '.join([*stack, goal_id])}")
        goal = self.goals.get(goal_id)
        if goal is None:
            raise RegistryError(f"unknown goal {goal_id}")
        local = {name: spec["type"] for name, spec in goal.parameters.items()}
        for name in local:
            _need_identifier(name, goal_id)
        for name in local.values():
            self._check_type(name, goal_id)
        outputs = set()
        for dep in goal.dependencies:
            if "core" in dep:
                core = self.cores.get(dep["core"])
                if core is None:
                    raise RegistryError(f"{goal_id}: unknown core {dep['core']}")
                expected = {k: (v["type"], v.get("required", True)) for k, v in core.inputs.items()}
                produced = core.output
            elif "goal" in dep:
                produced = self.goal_output_type(dep["goal"], (*stack, goal_id))
                child = self.goals[dep["goal"]]
                expected = {k: (v["type"], v.get("required", True)) for k, v in child.parameters.items()}
            else:
                raise RegistryError(f"{goal_id}: dependency needs 'core' or 'goal'")
            wiring = dep.get("input", {})
            for name, (type_name, required) in expected.items():
                if required and name not in wiring:
                    raise RegistryError(f"{goal_id}: required input '{name}' is not wired")
            for name, ref in wiring.items():
                if name not in expected:
                    raise RegistryError(f"{goal_id}: unknown input '{name}'")
                if isinstance(ref, dict):
                    continue  # literal
                if ref not in local:
                    raise RegistryError(f"{goal_id}: '{ref}' is not an earlier output or a parameter")
                if local[ref] != expected[name][0]:
                    raise RegistryError(
                        f"{goal_id}: '{ref}' is {local[ref]} but '{name}' needs {expected[name][0]}"
                    )
            out = _need(dep, "output", goal_id)
            _need_identifier(out, goal_id)
            if out in outputs or out in goal.parameters:
                raise RegistryError(f"{goal_id}: output name '{out}' is reused")
            outputs.add(out)
            local[out] = produced
        if goal.result not in outputs:
            raise RegistryError(f"{goal_id}: result '{goal.result}' is not an output")
        return local[goal.result]

    def bridge_of_kind(self, kind):
        return next(b for b in self.bridges if b.kind == kind)

    def core_by_name(self, name):
        return next((c for c in self.cores.values() if c.name == name), None)

    def providers_for(self, core_id):
        """Deterministic preference order: priority, then provider id."""
        options = [i for i in self.implementations.get(core_id, []) if i.available()]
        return sorted(options, key=lambda i: (i.priority, i.provider))
