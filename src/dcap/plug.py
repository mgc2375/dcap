"""Plug in an MCP server: read its tool list and write the definitions that wire it up.

Each tool becomes one plainly named use with its own switch. The names come from the
tool's own name and description, so nothing here is guessed by a model and the same
server always gives the same files. The server's name and its tool names stay on the
operator's side: an AI only ever sees the plain name, in pasted code.
"""
import keyword
import os
from pathlib import Path
import re
import shutil
import tempfile

import yaml

from . import operator
from .errors import StateError
from .registry import Registries, snake
from .resolver import Lexicon
from .runtime.remote import list_tools

# Words that say what is done, not what it is done to. They make poor topic keywords.
_VERBS = {
    "add", "call", "check", "close", "create", "delete", "describe", "do", "execute", "fetch", "find", "get", "list",
    "load", "make", "open", "post", "put", "query", "read", "remove", "run", "save", "search", "send", "set", "show",
    "start", "stop", "update", "write",
}
_HOSTS = {"target", "host", "hostname", "address", "addr", "ip", "domain", "network", "cidr", "subnet"}
_SCOPES = _HOSTS | {"url", "uri"}
_JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
_STATEFUL = {"loaded", "current", "session", "previous", "previously", "active", "opened"}
_NOT_NAMES = {"mcp", "server", "tool", "tools"}
_MAX_NAME_WORDS = 5


def store():
    """Where plugged servers are kept: with the operator's settings, never in a workspace."""
    return Path(os.environ.get("DCAP_PLUGGED") or operator.key_file().parent / "plugged")


def folders():
    """One definitions folder per plugged server, in name order."""
    root = store()
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []


def _words(text):
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]*|[0-9]+", spaced)]


def _identifier(text):
    name = snake(text)
    if not name or name[0].isdigit():
        name = f"t_{name}".rstrip("_")
    return f"{name}_" if keyword.iskeyword(name) else name


def _camel(text):
    return "".join(word.capitalize() for word in _words(text)) or "X"


def _first_sentence(text):
    return re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0].rstrip(".!?: ").strip() if text else ""


def _unique(name, taken):
    candidate, count = name, 2
    while candidate in taken:
        candidate, count = f"{name}_{count}", count + 1
    taken.add(candidate)
    return candidate


def _to_type(schema, shallow=False):
    """A JSON Schema as a DCAP type. Anything DCAP's type language can't say is left open."""
    if not isinstance(schema, dict):
        return {}
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    out = {"type": kind} if kind in _JSON_TYPES else {}
    values = schema.get("enum")
    if isinstance(values, list) and values and all(isinstance(v, (str, int, float, bool)) for v in values):
        out["enum"] = values
    if shallow:
        return out
    if kind == "array" and isinstance(schema.get("items"), dict):
        items = _to_type(schema["items"])
        if items:
            out["items"] = items
    if kind == "object" and isinstance(schema.get("properties"), dict) and schema["properties"]:
        out["properties"] = {str(name): _to_type(sub) for name, sub in schema["properties"].items()}
        required = [r for r in schema.get("required", []) if r in out["properties"]]
        if required:
            out["required"] = required
    return out


def _stem(word):
    for ending in ("ing", "es", "ed", "s"):
        if len(word) > len(ending) + 3 and word.endswith(ending):
            return word[: -len(ending)]
    return word


def _plain_name(tool, brand, class_words, lexicon):
    """What the tool does, in its own words: the tool's name, helped by its description."""
    told = _words(_first_sentence(tool.get("description") or ""))
    stems = {_stem(word) for word in told}
    verbs = _VERBS | lexicon.roots | set(lexicon.canonical)
    title = tool.get("title") or (tool.get("annotations") or {}).get("title")
    words = _words(title if isinstance(title, str) and title.strip() else tool["name"])
    # A vendor's name is dropped, unless the tool describes itself with it.
    words = [w for w in words if w not in brand or w in told] or words
    # So is a leading word the description never uses, when a later one is used: `nmap_scan` scans.
    while len(words) > 1 and words[0] not in verbs and _stem(words[0]) not in stems \
            and any(_stem(word) in stems for word in words[1:]):
        words = words[1:]
    meaning = [w for w in words if w not in lexicon.stopwords]
    for word in [*told, *class_words]:
        if len(meaning) >= 2:
            break
        if word not in lexicon.stopwords and word not in words and (word not in brand or word in class_words):
            words.append(word)
            meaning.append(word)
    return " ".join(words[:_MAX_NAME_WORDS]).capitalize()


def _aliases(name, tool, lexicon):
    """What an AI might say when it wants this. A phrase of one meaningful word is too easy to hit."""
    told = _first_sentence(tool.get("description") or "")
    options = [name, " ".join(_words(tool["name"])).capitalize()]
    if told and len(told.split()) <= 12:
        options.append(told)
    kept = []
    for option in options:
        if len(lexicon.tokens(option)) >= 2 and option.lower() not in {k.lower() for k in kept}:
            kept.append(option)
    return kept or [name]


def build(name, tools, command, registries, renames=None):
    """The definitions for one server. Returns (files, summary) and writes nothing."""
    cls = _identifier(name)
    if cls in registries.classes:
        raise StateError(f"'{cls}' is already the name of a class of capabilities")
    lexicon = Lexicon(registries.vocabulary)
    brand = {*_words(name), *_NOT_NAMES}
    class_words = [w for w in _words(name) if w not in _NOT_NAMES] or [cls]
    renames = renames or {}
    taken_names = {snake(c.name) for c in registries.cores.values()} | {snake(g.name) for g in registries.goals.values()}
    taken_ids, types, cores, goals, implements, uses, review = set(), {}, {}, [], {}, [], []
    keyword_counts = {}

    for tool in sorted((t for t in tools if isinstance(t.get("name"), str) and t["name"].strip()), key=lambda t: t["name"]):
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = set(schema.get("required") or []) if isinstance(schema.get("required"), list) else set()
        description = _first_sentence(tool.get("description") or "")

        plain = renames.get(tool["name"]) or _plain_name(tool, brand, class_words, lexicon)
        if _identifier(plain) != snake(plain):
            plain = f"Use {plain}"  # a name has to work as a Python name too
        for extra in (class_words[0], *map(str, range(2, 100))):
            if snake(plain) not in taken_names:
                break
            plain = f"{plain} {extra}"
        taken_names.add(snake(plain))
        # Everything an AI can see is made from the plain name, so the tool's own name never shows.
        ident = _unique(snake(plain), taken_ids)

        inputs, input_map, parameters, taken_params, scope = {}, {}, {}, set(), None
        for prop, sub in properties.items():
            param = _unique(_identifier(str(prop)), taken_params)
            shape = {**_to_type(sub), "aliases": sorted({str(prop), param})}
            type_name = f"{_camel(cls)}{_camel(str(prop))}"
            if types.get(type_name, shape) != shape:
                type_name = f"{_camel(cls)}{_camel(ident)}{_camel(str(prop))}"
            types[type_name] = shape
            spec = {"type": type_name} if prop in required else {"type": type_name, "required": False}
            inputs[param] = dict(spec)
            parameters[param] = dict(spec)
            if param != prop:
                input_map[param] = str(prop)
            holds_text = shape.get("type") == "string" or (shape.get("type") == "array" and shape.get("items") == {"type": "string"})
            if scope is None and prop in required and param in _SCOPES and holds_text:
                scope = param
                if param in _HOSTS and shape.get("type") == "string":
                    parameters[param]["extract"] = "target"

        output = f"{_camel(cls)}{_camel(ident)}Result"
        types[output] = _to_type(tool.get("outputSchema"), shallow=True)
        read_only = (tool.get("annotations") or {}).get("readOnlyHint") is True
        permission = f"{cls}.read" if read_only else f"{cls}.use"
        core_id = f"{cls}.{ident}"
        cores[core_id] = {
            "name": plain, "contract": 1, "description": description or plain,
            "inputs": inputs, "output": output, "permissions": [permission], **({"scope": scope} if scope else {}),
        }
        aliases = _aliases(plain, tool, lexicon)
        goals.append({
            "goal": f"{cls.upper()}.{ident.upper()}", "version": "1.0", "name": plain, "outcome": description or plain,
            "aliases": aliases, "parameters": parameters, "classes": [cls],
            "dependencies": [{"core": core_id, "input": {p: p for p in inputs}, "output": "result"}],
            "result": {"source": "result"},
        })
        implements[core_id] = {"contract": 1, "operation": tool["name"], **({"input_map": input_map} if input_map else {})}
        topic = {*_words(plain), *_words(tool["name"])} - _VERBS - lexicon.stopwords - set(lexicon.canonical) - lexicon.roots
        for word in {w for w in topic if len(w) > 2}:
            keyword_counts[word] = keyword_counts.get(word, 0) + 1
        uses.append({
            "tool": tool["name"], "name": plain, "switch": snake(plain).upper(), "heard_as": aliases,
            "permission": permission, **({"scope": scope} if scope else {}),
        })
        if len(lexicon.tokens(plain)) < 2:
            review.append(f"{tool['name']}: the name '{plain}' is one word, so it is only offered when asked for by name")
        if not properties and set(_words(tool.get("description") or "")) & _STATEFUL:
            review.append(f"{tool['name']}: may need another tool to run first; add a `before` step to its provider entry")

    if not cores:
        raise StateError("the server lists no tools")
    keywords = sorted(keyword_counts, key=lambda w: (-keyword_counts[w], w))[:12]
    keywords = list(dict.fromkeys([*class_words, *keywords]))
    capabilities = {
        "class": cls, "title": " ".join(class_words).capitalize(), "description": f"Uses wired from the {name} server.",
        "keywords": keywords, "expose_all_below": 8, "types": types, "cores": cores,
    }
    provider = {"providers": [{
        "provider": f"{cls}.mcp", "version": "1", "kind": "mcp", "priority": 50,
        "command": list(command), "text_output": "auto", "implements": implements,
    }]}
    header = f"# Written by `dcap plug` from the tool list of the {name} server. Edit freely: it is only rewritten by --replace.\n"
    files = {
        f"capabilities/{cls}.yaml": header + _dump(capabilities),
        f"goals/{cls}.yaml": header + _dump({"goals": goals}),
        f"providers/{cls}.yaml": header + _dump(provider),
    }
    permissions = sorted({use["permission"] for use in uses})
    return files, {"plugged": cls, "uses": uses, "grant": permissions, "review": review}


def _dump(document):
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=110)


def _absolute(command):
    """A server given by a path here is still found when DCAP runs somewhere else."""
    out = []
    for index, part in enumerate(command):
        is_path = "/" in part if index == 0 else not part.startswith("-")
        out.append(str(Path(part).resolve()) if is_path and not os.path.isabs(part) and Path(part).exists() else part)
    return out


def plug(name, command, definitions=(), replace=False, renames=None, simulate=None, timeout=30):
    """Read a server's tools, write its definitions, and check everything still loads together."""
    cls = _identifier(name)
    folder = store() / cls
    if folder.exists() and not replace:
        raise StateError(f"'{cls}' is already plugged in. Use --replace to wire it up again.")
    others = [f for f in folders() if f != folder]
    registries = Registries([*others, *definitions], simulate)
    command = _absolute(command)
    files, summary = build(name, list_tools(command, timeout), command, registries, renames)
    store().mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{cls}-", dir=store()))
    try:
        for rel, text in files.items():
            (staged / rel).parent.mkdir(parents=True, exist_ok=True)
            (staged / rel).write_text(text, encoding="utf-8")
        Registries([*others, *definitions, staged], simulate)  # a set of definitions that can't load is never kept
        if folder.exists():
            shutil.rmtree(folder)
        os.replace(staged, folder)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return {**summary, "folder": str(folder)}


def unplug(name):
    folder = store() / _identifier(name)
    if not folder.is_dir():
        raise StateError(f"'{_identifier(name)}' is not plugged in")
    shutil.rmtree(folder)
    return {"unplugged": folder.name}


def plugged(definitions=(), simulate=None):
    """What each plugged server gives: its plain names and the permissions to grant."""
    out = {}
    registries = Registries([*folders(), *definitions], simulate)
    for folder in folders():
        cores = [c for c in registries.cores.values() if c.cls == folder.name]
        out[folder.name] = {
            "uses": sorted(core.name for core in cores),
            "grant": sorted({p for core in cores for p in core.permissions}),
            "folder": str(folder),
        }
    return out
