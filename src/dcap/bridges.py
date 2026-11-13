"""Select a predefined bridge for existing code, and splice it in."""
import ast
import builtins
from dataclasses import dataclass
import re

from .observer import body_without_docstring, is_stub_statement, is_test_path, signature_hash

MIN_SCORE = 0.6
MIN_MARGIN = 0.05
FALLBACK_MAPPING_SCORE = 0.7
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


@dataclass
class BridgeMatch:
    bridge: object
    symbol: object
    mapping: dict  # facade param -> symbol param
    score: float
    guessed: bool = False  # a parameter was mapped only because it was the one left over


@dataclass
class Abstain:
    reason: str
    detail: str = ""
    candidates: tuple = ()


def source_lines(source):
    """Split as Python does. str.splitlines also breaks on form feeds and other separators."""
    return re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+", source)


def _map_params(symbol, facade_params, types):
    """Map facade params onto the symbol's. Returns (mapping, score) or None."""
    names = [name for name, _ in symbol.params]
    required = {name for name, has_default in symbol.params if not has_default}
    mapping, unmapped = {}, []
    for name, spec in facade_params.items():
        options = [name, *types[spec["type"]].get("aliases", [])]
        found = next((o for o in options if o in names and o not in mapping.values()), None)
        if found:
            mapping[name] = found
        elif spec["required"]:
            unmapped.append(name)
    score = 1.0
    if unmapped:
        free = [n for n in names if n not in mapping.values()]
        if len(unmapped) != 1 or len(free) != 1:
            return None
        mapping[unmapped[0]] = free[0]
        score = FALLBACK_MAPPING_SCORE
    if required - set(mapping.values()):
        return None  # the generated call could not supply a parameter the stub needs
    return mapping, score


def match_bridge(symbol, facade_params, registries):
    language = "python" if symbol.path.endswith(".py") else None
    bridges = [
        b for b in registries.bridges
        if b.language == language and b.kind == symbol.kind and b.is_async == symbol.is_async
    ]
    if not bridges:
        return None
    mapped = _map_params(symbol, facade_params, registries.types)
    if mapped is None:
        return None
    mapping, map_score = mapped
    bridge = max(bridges, key=lambda b: (b.score, b.id))
    score = 0.4 * bridge.score + 0.3 * map_score
    return BridgeMatch(bridge, symbol, mapping, score, map_score < 1.0)


def name_tokens(qualname, lexicon):
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", qualname).replace("_", " ").replace(".", " ")
    return lexicon.tokens(spaced)


def find_symbol(reference, symbols):
    """Resolve 'name', 'Class.method' or 'path.py:name'. Returns symbols that match."""
    path, _, name = reference.rpartition(":")
    return [s for s in symbols if s.qualname == name and (not path or s.path == path)]


def _is_test(symbol):
    return is_test_path(symbol.path) or symbol.qualname.rsplit(".", 1)[-1].startswith("test_")


def select_target(symbols, facade_params, vocabulary, registries, lexicon):
    """Pick an insertion point automatically, or abstain."""
    live = {}  # the last definition of a name is the one that exists at run time
    for symbol in symbols:
        key = (symbol.path, symbol.qualname)
        live[key] = max(live.get(key, 0), symbol.lineno)
    scored = []
    for symbol in symbols:
        if symbol.lineno != live[(symbol.path, symbol.qualname)]:
            continue
        if not symbol.fillable or _is_test(symbol):
            continue
        words = name_tokens(symbol.qualname, lexicon)
        overlap = len(words & vocabulary)
        if not overlap or words - vocabulary - lexicon.roots:
            continue  # shutdown_site shares a word with discovery but means something else
        match = match_bridge(symbol, facade_params, registries)
        if match and not match.guessed:
            match.score += 0.3 * min(1.0, overlap / 2)
            scored.append(match)
    scored.sort(key=lambda m: (-m.score, m.symbol.path, m.symbol.qualname))
    if not scored or scored[0].score < MIN_SCORE:
        return Abstain("no_compatible_symbol", "No unimplemented function or method fits this capability.")
    if len(scored) > 1 and scored[0].score - scored[1].score < MIN_MARGIN:
        names = tuple(f"{m.symbol.path}:{m.symbol.qualname}" for m in scored[:5])
        return Abstain("ambiguous_target", "Several symbols fit equally well; name one.", names)
    return scored[0]


def _locate(tree, qualname, lineno=None):
    def search(nodes, parts):
        for node in nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or node.name != parts[0]:
                continue
            if len(parts) > 1:
                found = search(node.body, parts[1:])
                if found:
                    return found
            elif lineno is None or node.lineno == lineno:
                return node
        return None

    node = search(tree.body, qualname.split("."))
    if node is None:
        raise LookupError(qualname)
    return node


def markers(integration_id):
    return f"# dcap:begin {integration_id}", f"# dcap:end {integration_id}"


def render(match, module, os_clause, switches):
    args = ", ".join(f"{facade}={param}" for facade, param in match.mapping.items())
    return fill(match.bridge, module=module, args=args, os=os_clause, switches=switches)


def fill(bridge, **values):
    """Render a predefined template. Nothing outside its placeholders can change."""
    text = bridge.template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text.strip().splitlines()


def dual_import(lines, package):
    """Make an import work beside a file however that file is loaded, as a script or as a module."""
    out = []
    for line in lines:
        found = re.fullmatch(rf"(\s*)from ({re.escape(package)}(?:\.\S+)?) import (.+)", line)
        if not found:
            out.append(line)
            continue
        indent, module, names = found.groups()
        out += [
            f"{indent}if __spec__ is not None and __spec__.parent:",
            f"{indent}    from .{module} import {names}",
            f"{indent}else:",
            f"{indent}    from {module} import {names}",
        ]
    return out


def _bound_names(tree):
    """Names the file binds. A reference to something it never binds does not take the name."""
    names, used, star = set(dir(builtins)), set(), False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            (used if isinstance(node.ctx, ast.Load) else names).add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            star = star or any(a.name == "*" for a in node.names)
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names | used if star else names


def free_name(source, wanted):
    """A variable name the file does not already bind."""
    taken = _bound_names(ast.parse(source))
    name, n = wanted, 2
    while name in taken:
        name, n = f"{wanted}_{n}", n + 1
    return name


def free_names(source, wanted, reserved=()):
    """A name for each wanted one that the file does not bind, in the order given."""
    taken = _bound_names(ast.parse(source)) | set(reserved)
    names = []
    for base in wanted:
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        taken.add(name)
        names.append(name)
    return names


def os_import(source):
    """How a block imports os here: (import clause, name), never rebinding another `os`."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import) and any(a.name == "os" and not a.asname for a in node.names):
            return "os", "os"
    name = free_name(source, "os")
    return ("os", "os") if name == "os" else (f"os as {name}", name)


def switch_line(os_name, switch, value="false", note=""):
    line = f'{os_name}.environ["{switch}"] = "{value}"'
    return f"{line}  # {note}" if note else line


def _regions(lines):
    """(begin, end) line indexes of every DCAP region in the file."""
    begins = {}
    for index, line in enumerate(lines):
        kind = re.fullmatch(r"\s*# dcap:(begin|end) (\S+)\s*", line)
        if kind and kind[1] == "begin":
            begins[kind[2]] = index
        elif kind and kind[2] in begins:
            yield begins.pop(kind[2]), index


def insert_block(source, body_lines, integration_id):
    """Add a marked block after the file's docstring and leading imports."""
    tree = ast.parse(source)
    lines = source_lines(source)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    anchor = 0
    for index, node in enumerate(tree.body):
        is_doc = index == 0 and ast.get_docstring(tree, clean=False) is not None
        if is_doc or isinstance(node, (ast.Import, ast.ImportFrom)):
            anchor = node.end_lineno
        else:
            break
    if anchor == 0:  # keep a shebang or encoding comment first
        for index, line in enumerate(lines[:2]):
            if _CODING_COOKIE.match(line):
                anchor = index + 1
        while anchor < len(lines) and lines[anchor].startswith("#"):
            anchor += 1
    for node in tree.body:  # never split a line shared with the next statement
        if node.lineno <= anchor:
            anchor = max(anchor, node.end_lineno)
    for start, stop in _regions(lines):  # never land inside another integration's region
        if start < anchor <= stop:
            anchor = stop + 1
    if anchor and lines[anchor - 1].rstrip("\r\n") == lines[anchor - 1]:
        lines[anchor - 1] += newline  # the anchor line had no line ending
    begin, end = markers(integration_id)
    region = [f"{text}{newline}" for text in [begin, *body_lines, end]]
    result = "".join([*lines[:anchor], *region, *lines[anchor:]])
    before = [ast.dump(node) for node in tree.body]
    added = [ast.dump(node) for node in ast.parse("\n".join(body_lines)).body]
    split = sum(1 for node in tree.body if node.end_lineno <= anchor)
    if [ast.dump(node) for node in ast.parse(result).body] != [*before[:split], *added, *before[split:]]:
        raise ValueError("a block here would change existing code")
    return result


def _inner_indent(definition_line):
    outer = definition_line[:len(definition_line) - len(definition_line.lstrip(" \t\f"))]
    return outer + ("\t" if "\t" in outer else "    ")


def splice(source, qualname, body_lines, integration_id, lineno=None):
    """Replace a stub's body with a marked region. Returns (new_source, original_fragment)."""
    lines = source_lines(source)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    tree = ast.parse(source)
    node = _locate(tree, qualname, lineno)
    stubs = body_without_docstring(node)
    if not all(is_stub_statement(stmt) for stmt in stubs):
        raise ValueError(f"{qualname} has an implementation")
    begin, end = markers(integration_id)

    def region(indent):
        return [f"{indent}{text}{newline}" for text in [begin, *body_lines, end]]

    if not stubs:  # docstring only
        last = node.body[-1]
        first_line = lines[last.lineno - 1]
        head = first_line.encode()[:last.col_offset].decode()
        kept = list(lines[last.lineno - 1:last.end_lineno])
        if kept[-1].rstrip("\r\n") == kept[-1]:
            kept[-1] += newline  # the docstring ended the file with no line ending
        if head.strip():  # `def f(x): """doc"""` needs the docstring on its own line first
            indent = _inner_indent(lines[node.lineno - 1])
            kept[0] = indent + kept[0].encode()[last.col_offset:].decode()
            kept.insert(0, head.rstrip() + newline)
        else:
            indent = head
        new, original = [*lines[:last.lineno - 1], *kept, *region(indent), *lines[last.end_lineno:]], []
    else:
        first, last = stubs[0], stubs[-1]
        head = lines[first.lineno - 1].encode()[:first.col_offset].decode()
        if head.strip():  # one-line definition: `def f(x): pass`
            original = [lines[first.lineno - 1].encode()[first.col_offset:].decode().rstrip("\r\n")]
            indent = _inner_indent(lines[node.lineno - 1])
            new = [*lines[:first.lineno - 1], head.rstrip() + newline, *region(indent), *lines[last.end_lineno:]]
        else:
            original = [
                line.rstrip("\r\n").removeprefix(head) for line in lines[first.lineno - 1:last.end_lineno]
            ]
            new = [*lines[:first.lineno - 1], *region(head), *lines[last.end_lineno:]]
    result = "".join(new)
    after = ast.parse(result)
    changed = _locate(after, qualname, node.lineno)
    node.body, changed.body = [], []
    if ast.dump(tree) != ast.dump(after):
        raise ValueError(f"filling {qualname} would change other code")
    return result, original


def signature_of(source, qualname, lineno=None):
    return signature_hash(_locate(ast.parse(source), qualname, lineno))
