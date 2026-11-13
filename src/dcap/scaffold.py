"""Draft a provider definition from a live MCP server, for an operator to review."""
import yaml

from .bridges import name_tokens
from .runtime.remote import list_tools


def _match_inputs(core, tool, types):
    """Map core inputs to tool properties. Returns (input_map, unresolved required inputs)."""
    properties = tool.get("inputSchema", {}).get("properties", {})
    input_map, unresolved = {}, []
    for name, spec in core.inputs.items():
        options = [name, *types[spec["type"]].get("aliases", [])]
        found = next((o for o in options if o in properties), None)
        if found is None:
            if spec.get("required", True):
                unresolved.append(name)
        elif found != name:
            input_map[name] = found
    return input_map, unresolved


def draft(name, command, registries, lexicon, timeout=30):
    """Propose a core for a tool only when the tool covers every word of the core's name.

    Each tool goes to its single best core. Words in the tool's name count for more
    than words in its description, which often mention neighbouring concepts.
    """
    tools = list_tools(command, timeout)
    best_for_core = {}
    for tool in tools:
        name_words = name_tokens(tool["name"], lexicon)
        words = name_words | lexicon.tokens(tool.get("description", ""))
        options = []
        for core in registries.cores.values():
            wanted = lexicon.tokens(core.name)
            if wanted <= words:
                rank = (len(wanted & name_words), len(words & lexicon.tokens(core.description)))
                options.append((rank, core.id))
        if not options:
            continue
        rank, core_id = max(options, key=lambda item: (item[0], item[1]))
        held = best_for_core.get(core_id)
        if held is None or (rank, tool["name"]) > (held[0], held[1]["name"]):
            best_for_core[core_id] = (rank, tool)

    implements, review, used = {}, [], set()
    for core_id in sorted(best_for_core):
        tool = best_for_core[core_id][1]
        core = registries.cores[core_id]
        input_map, unresolved = _match_inputs(core, tool, registries.types)
        entry = {"contract": core.contract, "operation": tool["name"]}
        if input_map:
            entry["input_map"] = input_map
        implements[core_id] = entry
        used.add(tool["name"])
        if unresolved:
            review.append(f"{core_id}: map required inputs {unresolved} for {tool['name']}")
    unmapped = sorted(t["name"] for t in tools if t["name"] not in used)
    return {
        "definition": {"providers": [{
            "provider": name, "kind": "mcp", "version": "0", "priority": 50,
            "command": list(command), "implements": implements,
        }]},
        "review": review,
        "unmapped_tools": unmapped,
    }


def render(result):
    lines = [
        "# Draft from a live server. Review before using it. Mappings are matched on names only.",
        "# Set output_key where a tool wraps its result, and confirm each output fits the core's type.",
    ]
    lines += [f"# REVIEW {item}" for item in result["review"]]
    lines += [f"# not mapped: {tool}" for tool in result["unmapped_tools"]]
    return "\n".join(lines) + "\n" + yaml.safe_dump(result["definition"], sort_keys=False)
