"""Execution engine: validate, authorise, then run a plan level by level."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from importlib import import_module
import time

from .runtime.policy import NotAuthorized, authorize, granted
from .runtime.remote import call_remote
from .schema import conform, validate

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 1


def call_provider(impl, inputs, timeout, grants=None):
    present = {k: v for k, v in inputs.items() if v is not None}
    if impl.kind == "native":
        module, _, attr = impl.operation.partition(":")
        operation = getattr(import_module(module), attr)
        if grants is None:
            return operation(**present)
        with granted(grants):  # a capability's own checks use this run's grants
            return operation(**present)
    return call_remote(remote_spec(impl), present, timeout)


def remote_spec(impl):
    spec = {"kind": impl.kind, "input_map": impl.input_map, "output_key": impl.output_key}
    if impl.kind == "mcp":
        spec.update(command=impl.command, tool=impl.operation)
        if impl.before:
            spec["before"] = impl.before
        if impl.text_output:
            spec["text_output"] = "auto" if impl.text_output == "auto" else True
    elif impl.kind == "http":
        spec.update(url=impl.url.rstrip("/") + impl.operation, method=impl.method, headers_env=impl.headers_env)
    else:
        raise ValueError(f"no adapter for provider kind {impl.kind}")
    return spec


def required_permissions(plan, registries):
    return sorted({p for s in plan.steps for p in registries.cores[s.core].permissions})


class Engine:
    def __init__(self, registries, grants):
        self.reg = registries
        self.grants = grants

    def run(self, plan, params, debug=False):
        problems = self._check_params(plan, params)
        if problems:
            return {"status": "failed", "errors": [{"code": "INVALID_INPUT", "message": p} for p in problems]}
        denied = self.preflight(plan, params)
        if denied:
            return denied

        values, errors, trace, outcome = {}, [], [], {}
        for level in plan.levels():
            with ThreadPoolExecutor(max_workers=len(level)) as pool:
                futures = {s.id: pool.submit(self._step, s, params, values, outcome, trace) for s in level}
            for step in level:
                ok, value = futures[step.id].result()
                if ok:
                    values[step.id] = value
                    outcome[step.id] = "ok"
                else:
                    outcome[step.id] = value["status"]
                    errors.append(value["error"])

        result = {"status": self._overall(outcome)}
        if plan.result in values:
            result["result"] = values[plan.result]
        elif values:
            result["completed"] = values
        if errors:
            result["errors"] = errors
        result["steps"] = {self.reg.cores[s.core].name: outcome[s.id] for s in plan.steps}
        if debug:
            result["trace"] = trace
        return result

    def preflight(self, plan, params):
        """Check every stage's permissions before anything runs. Returns a refusal or None."""
        missing, scopes = set(), set()
        for step in plan.steps:
            core = self.reg.cores[step.core]
            ref = step.inputs.get(core.scope) if core.scope else None
            value = None
            if ref and ref[0] == "param":
                value = params.get(ref[1])
            elif ref and ref[0] == "literal":
                value = ref[1]
            for permission in core.permissions:
                try:
                    authorize(permission, value, self.grants)
                except NotAuthorized as exc:
                    missing.add(permission)
                    if exc.scope:
                        scopes.add(exc.scope)
        if not missing:
            return None
        refusal = {
            "status": "authorization_required",
            "permissions": sorted(missing),
            "how": "An operator must add these to the DCAP policy file.",
        }
        if scopes:
            refusal["scope"] = sorted(scopes)
        return refusal

    @staticmethod
    def _overall(outcome):
        states = set(outcome.values())
        if states == {"ok"}:
            return "complete"
        return "partial" if "ok" in states else "failed"

    def _check_params(self, plan, params):
        problems = [f"unknown parameter '{n}'" for n in params if n not in plan.params]
        for name, spec in plan.params.items():
            if name in params:
                problems += validate(params[name], self.reg.types[spec["type"]], self.reg.types, name)
            elif spec["required"]:
                problems.append(f"missing parameter '{name}'")
        return problems

    def _step(self, step, params, values, outcome, trace):
        core = self.reg.cores[step.core]
        if any(outcome.get(dep) != "ok" for dep in step.depends_on):
            return False, {"status": "skipped", "error": {
                "code": "DEPENDENCY_FAILED", "capability": core.name,
                "message": "skipped because an earlier stage did not complete",
            }}
        inputs = {}
        for name, ref in step.inputs.items():
            if ref[0] == "step":
                inputs[name] = values[ref[1]]
            elif ref[0] == "literal":
                inputs[name] = ref[1]
            elif ref[1] in params:
                inputs[name] = params[ref[1]]

        problems = []
        for name, spec in core.inputs.items():
            if name in inputs:
                problems += validate(inputs[name], self.reg.types[spec["type"]], self.reg.types, name)
            elif spec.get("required", True):
                problems.append(f"missing input '{name}'")
        if problems:
            return False, {"status": "failed", "error": {
                "code": "INVALID_INPUT", "capability": core.name, "message": "; ".join(problems),
            }}

        for permission in core.permissions:
            # Checked again here because earlier stages can produce new targets.
            try:
                authorize(permission, inputs.get(core.scope) if core.scope else None, self.grants)
            except NotAuthorized as exc:
                return False, {"status": "failed", "error": {
                    "code": "UNAUTHORIZED", "capability": core.name, "message": str(exc),
                }}

        providers = self.reg.providers_for(step.core)
        if not providers:
            return False, {"status": "failed", "error": {
                "code": "PROVIDER_UNAVAILABLE", "capability": core.name, "message": "no compatible provider is available",
            }}

        timeout = core.execution.get("timeout_s", DEFAULT_TIMEOUT)
        retries = core.execution.get("retries", DEFAULT_RETRIES)
        code = "EXECUTION_FAILED"
        for impl in providers:
            for attempt in range(retries + 1):
                started = time.monotonic()
                try:
                    value = self._call(impl, inputs, timeout, self.grants)
                    bad = validate(value, self.reg.types[core.output], self.reg.types)
                    if bad:
                        code = "INVALID_OUTPUT"
                        raise ValueError("; ".join(bad))
                    trace.append(self._entry(step, impl, attempt, started, "ok"))
                    # Results keep one shape, so a provider's own fields never reach the AI.
                    return True, conform(value, self.reg.types[core.output], self.reg.types)
                except FutureTimeout:
                    code = "TIMEOUT"
                    trace.append(self._entry(step, impl, attempt, started, "timeout"))
                except Exception as exc:  # provider faults stay behind the interface
                    if not isinstance(exc, ValueError) or code != "INVALID_OUTPUT":
                        code = "EXECUTION_FAILED"
                    trace.append(self._entry(step, impl, attempt, started, f"error: {exc}"))
        return False, {"status": "failed", "error": {
            "code": code, "capability": core.name, "message": "no provider could complete this capability",
        }}

    @staticmethod
    def _call(impl, inputs, timeout, grants=None):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(call_provider, impl, inputs, timeout, grants).result(timeout=timeout)
        finally:
            pool.shutdown(wait=False)

    @staticmethod
    def _entry(step, impl, attempt, started, result):
        return {"step": step.id, "provider": impl.provider, "attempt": attempt,
                "seconds": round(time.monotonic() - started, 3), "result": result}
