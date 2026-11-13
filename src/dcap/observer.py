"""Passive project observer. It reads the workspace and never writes to it."""
import ast
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import tomllib

IGNORED = {
    ".git", ".dcap", ".venv", "venv", ".tox", ".nox", "site-packages", "node_modules", "__pycache__",
    "dcap_capabilities", "dist", "build",
}
LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".scala": "scala", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "c++", ".cpp": "c++", ".hpp": "c++", ".cs": "c#", ".swift": "swift",
    ".m": "objective-c", ".lua": "lua", ".pl": "perl", ".r": "r", ".dart": "dart", ".ex": "elixir",
    ".clj": "clojure", ".hs": "haskell", ".ml": "ocaml", ".sh": "shell",
}


def _is_environment(folder):
    """A virtual environment of any name holds installed code, not the project's."""
    return (folder / "pyvenv.cfg").exists() or (folder / "conda-meta").is_dir()


@dataclass
class Symbol:
    qualname: str
    kind: str  # function | method
    path: str
    lineno: int
    params: list  # [(name, has_default)]
    is_async: bool
    is_stub: bool
    signature: str
    fillable: bool = False  # a stub meant to be implemented, not just declared


def is_test_path(rel):
    parts = rel.split("/")
    name = parts[-1]
    return (
        bool({"test", "tests"} & set(parts[:-1]))
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name in {"conftest.py", "setup.py"}
    )


def is_stub_statement(stmt):
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        target = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return isinstance(target, ast.Name) and target.id == "NotImplementedError"
    return False


def body_without_docstring(node):
    body = node.body
    return body[1:] if ast.get_docstring(node, clean=False) is not None else body


def signature_hash(node):
    text = f"{type(node).__name__}|{ast.unparse(node.args)}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _params(node):
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    result = [(a.arg, i >= first_default) for i, a in enumerate(positional)]
    result += [(a.arg, d is not None) for a, d in zip(args.kwonlyargs, args.kw_defaults)]
    return result


def _name_of(expr):
    if isinstance(expr, ast.Subscript):
        expr = expr.value
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return expr.id if isinstance(expr, ast.Name) else ""


def _is_declaration(node, owner):
    """Abstract methods, overloads and Protocol members are stubs by design."""
    if {_name_of(d) for d in node.decorator_list} & {"abstractmethod", "overload"}:
        return True
    return owner is not None and "Protocol" in {_name_of(b) for b in owner.bases}


def _symbol(node, qualname, kind, rel, owner=None):
    params = _params(node)
    if kind == "method" and params and params[0][0] in {"self", "cls"}:
        params = params[1:]
    stub = all(is_stub_statement(s) for s in body_without_docstring(node))
    special = node.name.startswith("__") and node.name.endswith("__")  # a bridge's `return` can't serve these
    return Symbol(
        qualname, kind, rel, node.lineno, params, isinstance(node, ast.AsyncFunctionDef),
        stub, signature_hash(node), stub and not special and not _is_declaration(node, owner),
    )


def parse_symbols(source, rel):
    symbols = []
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_symbol(node, node.name, "function", rel))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_symbol(item, f"{node.name}.{item.name}", "method", rel, node))
    return symbols


class Observer:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._files = {}  # rel -> (stamp, symbols)
        self._languages = Counter()
        self._dependencies = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def refresh(self):
        seen, languages, dependencies = {}, Counter(), set()
        for folder, subdirs, names in os.walk(self.root):
            subdirs[:] = [d for d in subdirs if d not in IGNORED and not _is_environment(Path(folder) / d)]
            for name in names:
                path = Path(folder) / name
                rel = path.relative_to(self.root).as_posix()
                language = LANGUAGES.get(path.suffix)
                if language:
                    languages[language] += 1
                if name in {"pyproject.toml", "requirements.txt", "package.json"}:
                    dependencies |= self._read_dependencies(path)
                if path.suffix != ".py":
                    continue
                try:
                    stat = path.stat()
                except OSError:  # removed while walking
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._files.get(rel)
                if cached and cached[0] == stamp:
                    seen[rel] = cached
                    continue
                try:
                    seen[rel] = (stamp, parse_symbols(path.read_text(encoding="utf-8"), rel))
                except (SyntaxError, UnicodeDecodeError, ValueError, OSError, RecursionError, MemoryError):
                    seen[rel] = (stamp, None)  # unreadable for now, which says nothing about its symbols
        with self._lock:
            self._files, self._languages, self._dependencies = seen, languages, dependencies
        return self

    @staticmethod
    def _read_dependencies(path):
        try:
            if path.name == "package.json":
                data = json.loads(path.read_text())
                found = {*data.get("dependencies", {}), *data.get("devDependencies", {})}
                return {name for name in found if isinstance(name, str)}
            if path.name == "pyproject.toml":
                listed = tomllib.loads(path.read_text()).get("project", {}).get("dependencies", [])
            else:
                listed = [l for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]
            return {re.split(r"[\s<>=!~\[;]", item.strip(), maxsplit=1)[0] for item in listed}
        except (ValueError, OSError, AttributeError, TypeError):  # any file can have any shape
            return set()

    def recent_python(self):
        """Non-test Python files, most recently changed first: the code being worked on."""
        with self._lock:
            files = [(rel, entry[0][0]) for rel, entry in self._files.items() if not is_test_path(rel)]
        return [rel for rel, _ in sorted(files, key=lambda item: (-item[1], item[0]))]

    def symbols(self):
        with self._lock:
            return [symbol for _, symbols in self._files.values() for symbol in symbols or []]

    def unparsed(self, rel):
        """True while a file exists but can't be parsed, as it can be mid-edit."""
        with self._lock:
            return rel in self._files and self._files[rel][1] is None

    def snapshot(self):
        with self._lock:
            return {
                "languages": dict(self._languages),
                "dependencies": sorted(self._dependencies),
                "symbols": sum(len(s or []) for _, s in self._files.values()),
            }

    def start(self, interval=2.0):
        if self._thread:
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="dcap-observer")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
