"""Compile goal definitions into a flat, deduplicated execution plan."""
from dataclasses import dataclass, field
import json

from .errors import RegistryError


@dataclass
class Step:
    id: str
    core: str
    inputs: dict  # input name -> ("param", name) | ("step", id) | ("literal", value)

    @property
    def depends_on(self):
        return [ref[1] for ref in self.inputs.values() if ref[0] == "step"]


@dataclass
class Plan:
    subject: str
    version: str
    params: dict  # name -> {type, required}
    steps: list = field(default_factory=list)
    result: str = ""

    def levels(self):
        """Group steps so each level depends only on earlier levels."""
        done, remaining, levels = set(), list(self.steps), []
        while remaining:
            ready = [s for s in remaining if set(s.depends_on) <= done]
            if not ready:
                raise RegistryError(f"{self.subject}: unresolvable plan")
            levels.append(ready)
            done |= {s.id for s in ready}
            remaining = [s for s in remaining if s.id not in done]
        return levels


class Planner:
    def __init__(self, registries):
        self.reg = registries

    def for_goal(self, goal_id):
        goal = self.reg.goals[goal_id]
        params = {n: {"type": s["type"], "required": s.get("required", True)} for n, s in goal.parameters.items()}
        plan = Plan(goal_id, goal.version, params)
        env = {name: ("param", name) for name in goal.parameters}
        memo = {}
        plan.result = self._compile(goal, env, plan, memo)[1]
        return plan

    def for_core(self, core_id):
        core = self.reg.cores[core_id]
        params = {n: {"type": s["type"], "required": s.get("required", True)} for n, s in core.inputs.items()}
        plan = Plan(core_id, str(core.contract), params)
        plan.steps.append(Step("_output", core_id, {n: ("param", n) for n in core.inputs}))
        plan.result = "_output"
        return plan

    def _compile(self, goal, env, plan, memo):
        local = dict(env)
        for dep in goal.dependencies:
            inputs = {}
            for name, ref in dep.get("input", {}).items():
                bound = ("literal", ref["literal"]) if isinstance(ref, dict) else local.get(ref)
                if bound is not None:
                    inputs[name] = bound
            if "core" in dep:
                local[dep["output"]] = ("step", self._add(dep["core"], inputs, dep["output"], plan, memo))
            else:
                local[dep["output"]] = self._compile(self.reg.goals[dep["goal"]], inputs, plan, memo)
        return local[goal.result]

    @staticmethod
    def _add(core_id, inputs, wanted, plan, memo):
        key = json.dumps([core_id, inputs], sort_keys=True, default=str)
        if key in memo:
            return memo[key]
        # Step ids become local variables in generated code.
        taken = {s.id for s in plan.steps} | set(plan.params) | {"run", "self", "authorize", "call_remote", "checked_call"}
        step_id, n = wanted, 2
        while step_id in taken:
            step_id, n = f"{wanted}_{n}", n + 1
        plan.steps.append(Step(step_id, core_id, inputs))
        memo[key] = step_id
        return step_id
