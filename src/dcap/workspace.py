"""Transactional workspace changes, ownership tracking and rollback."""
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # no advisory locks on this platform
    fcntl = None

from .bridges import markers, signature_of, source_lines
from .errors import ConflictError, StateError, ValidationFailure

STATE = ".dcap"
KEEP_RESULTS = 100
LOCK_TIMEOUT = 60  # longer than one validation, which can take up to 30 seconds
_SWITCH_VALUE = re.compile(r"""(\.environ\[["'][A-Z0-9_]+["']\]\s*=\s*)["'](?:true|false)["']""", re.IGNORECASE)
_SWITCH_LINE = re.compile(r"""\.environ\[["']([A-Z0-9_]+)["']\]\s*=\s*["'](true|false)["']""", re.IGNORECASE)
_UNKNOWN = object()


def sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _read(path):
    return path.read_bytes().decode("utf-8")


def _digest(path):
    """The hash of a path's bytes, None if there is nothing there, or a mark that it can't be read."""
    try:
        return sha(path.read_bytes())
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "unreadable"


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = self.root / STATE
        self.recover()

    @property
    def _manifest_path(self):
        return self.state / "manifest.json"

    def manifest(self):
        if not self._manifest_path.exists():
            return {"version": 1, "integrations": {}}
        try:
            data = json.loads(self._manifest_path.read_text())
        except ValueError as exc:
            raise StateError(f"{self._manifest_path} is corrupt: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("integrations"), dict):
            raise StateError(f"{self._manifest_path} is corrupt: no integrations")
        return data

    def resolve(self, rel):
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise ConflictError(f"path escapes the workspace: {rel}")
        return path

    def read(self, rel):
        return _read(self.resolve(rel))

    # Results ---------------------------------------------------------------
    def save_result(self, request_id, data):
        results = self.state / "results"
        results.mkdir(parents=True, exist_ok=True)
        temp = results / f"{request_id}.tmp"
        temp.write_text(json.dumps(data, default=str))
        os.replace(temp, results / f"{request_id}.json")
        try:
            kept = sorted(results.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            for old in kept[KEEP_RESULTS:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass  # another process is pruning too; the result itself is saved

    def load_result(self, request_id):
        if not re.fullmatch(r"r[0-9a-f]{12}", str(request_id)):
            return None
        path = self.state / "results" / f"{request_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError as exc:
            raise StateError(f"{path} is corrupt: {exc}") from exc

    def recovered(self):
        """Files set aside because a crashed transaction found later edits in them."""
        folder = self.state / "recovered"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    # Locking ---------------------------------------------------------------
    @contextmanager
    def _exclusive(self):
        """Hold the workspace against other DCAP processes. Released by the OS if this one dies."""
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "lock", "a") as handle:
            if fcntl:
                deadline = time.monotonic() + LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ConflictError("another DCAP process is changing this workspace") from None
                        time.sleep(0.02)
            yield

    # Transaction core ------------------------------------------------------
    def _write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        try:
            current = path.stat()
        except FileNotFoundError:
            current = None
        if current and current.st_nlink > 1:
            path.write_bytes(data)  # replacing the file would break its other links
            return
        temp = path.with_name(path.name + ".dcap-tmp")
        mode = stat.S_IMODE(current.st_mode) if current else 0o666
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        if current:
            os.chmod(temp, mode)  # the umask must not change an existing file's permissions
        os.replace(temp, path)

    def _transact(self, writes, manifest, region=None, modes=None):
        """Apply {rel: text | None} and the manifest atomically, with a journal for crashes.

        The journal records what each write will produce, so recovery can tell DCAP's
        own unfinished work from an edit someone saved after the crash. The caller holds the lock.
        """
        tx = self.state / "tx"
        tx.mkdir(parents=True, exist_ok=True)
        entries = []
        targets = {**writes, f"{STATE}/manifest.json": json.dumps(manifest, indent=2, sort_keys=True) + "\n"}
        for index, (rel, content) in enumerate(targets.items()):
            path = self.resolve(rel)
            saved = None
            if path.exists():
                saved = str(index)
                shutil.copy2(path, tx / saved)
            entries.append([rel, saved, None if content is None else sha(content), region])
        journal = self.state / "journal.json"
        temp = journal.with_suffix(".tmp")
        temp.write_text(json.dumps(entries))
        os.replace(temp, journal)
        try:
            for rel, content in targets.items():
                path = self.resolve(rel)
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    self._write(path, content)
                    if modes and rel in modes:
                        os.chmod(path, modes[rel])
        except BaseException:
            self._undo()
            raise
        journal.unlink()
        shutil.rmtree(tx, ignore_errors=True)
        self._prune(rel for rel, content in targets.items() if content is None)

    def recover(self):
        """Undo a transaction whose process died. A live transaction holds the lock, so it is left alone."""
        if not self.state.is_dir():
            return
        try:
            handle = open(self.state / "lock", "a")
        except OSError:
            return
        with handle:
            if fcntl:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
            self._undo()

    def _undo(self):
        journal = self.state / "journal.json"
        if not journal.exists():
            shutil.rmtree(self.state / "tx", ignore_errors=True)
            return
        try:
            entries = json.loads(journal.read_text())
        except ValueError as exc:
            raise StateError(f"{journal} is corrupt: {exc}") from exc
        failed = []
        for rel, saved, *rest in entries:
            try:
                self._restore(rel, saved, *rest)
            except OSError as exc:
                failed.append(f"{rel}: {exc}")
        if failed:
            raise StateError(f"could not undo an interrupted change: {'; '.join(failed)}")
        journal.unlink()
        shutil.rmtree(self.state / "tx", ignore_errors=True)
        self._prune(entry[0] for entry in entries if entry[1] is None)

    def _restore(self, rel, saved, after=_UNKNOWN, region=None):
        path = self.resolve(rel)
        temp = path.with_name(path.name + ".dcap-tmp")
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        original = self.state / "tx" / saved if saved is not None else None
        if after is not _UNKNOWN:
            current = _digest(path)
            if current == (_digest(original) if original else None):
                return  # the transaction never reached this file
            if current != after:
                self._keep_later_edit(rel, path, original, region)
                return
        if original is None:
            try:
                path.unlink(missing_ok=True)
            except NotADirectoryError:
                pass  # a parent is a file, so there is nothing here to remove
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, path)

    def _keep_later_edit(self, rel, path, original, region):
        """Someone edited this file after the crash. Their work stays; only DCAP's own region leaves."""
        if original:
            folder = self.state / "recovered"
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, folder / rel.replace("/", "__"))
        if not region or region["path"] != rel:
            return
        begin, end = markers(region["id"])
        try:
            current = _read(path)
            if self._region_hash(current, begin, end) == region["hash"]:
                restored = self._remove_region(current, region["id"], region["fragment"])
                if restored is not None:
                    self._write(path, restored)
        except (ValueError, SyntaxError):
            pass

    def _prune(self, removed):
        """Remove a managed package folder once only caches are left in it."""
        folders = {self.resolve(rel).parent for rel in removed}
        for folder in folders:
            if folder.name == "dcap_capabilities" and folder.is_dir():
                if {p.name for p in folder.iterdir()} <= {"__pycache__"}:
                    shutil.rmtree(folder, ignore_errors=True)

    def held_by_others(self, integration_id, files):
        """Generated files another integration needs at a different version."""
        others = {
            rel: digest
            for other, entry in self.manifest()["integrations"].items() if other != integration_id
            for rel, digest in entry["files"].items()
        }
        return sorted(rel for rel, content in files.items() if rel in others and others[rel] != sha(content))

    # Integrate -------------------------------------------------------------
    def integrate(self, integration_id, record, files, edit):
        """Commit generated files plus at most one edit to existing code.

        edit = {path, symbol, source_before, source_after, fragment, signature, lineno}, or None.
        symbol is None when the edit adds a block rather than filling a stub.
        """
        with self._exclusive():
            manifest = self.manifest()
            if integration_id in manifest["integrations"]:
                raise ConflictError(f"integration {integration_id} already exists")
            owned = {
                rel: digest
                for entry in manifest["integrations"].values()
                for rel, digest in entry["files"].items()
            }
            writes = {}
            for rel, content in files.items():
                path = self.resolve(rel)
                if path.exists():
                    current = sha(path.read_bytes())
                    if rel not in owned:
                        raise ConflictError(f"{rel} exists and is not owned by DCAP")
                    if current != owned[rel]:
                        raise ConflictError(f"{rel} was modified outside DCAP")
                    if current != sha(content):
                        raise ConflictError(f"{rel} is managed at a different version; update first")
                else:
                    writes[rel] = content

            stored_edit, region, modes = None, None, None
            if edit:
                target = self.resolve(edit["path"])
                self._check_unchanged(target, edit)
            self._validate(files, edit)
            if edit:
                # Validation takes time, and the file may have been saved again meanwhile.
                self._check_unchanged(target, edit)
                backup = f"{STATE}/backups/{integration_id}.orig"
                begin, end = markers(integration_id)
                digest = self._region_hash(edit["source_after"], begin, end)
                if digest is None:
                    raise ValidationFailure(f"the DCAP markers could not be placed in {edit['path']}")
                writes[edit["path"]] = edit["source_after"]
                writes[backup] = edit["source_before"]
                modes = {backup: stat.S_IMODE(target.stat().st_mode)}  # a copy is as private as its original
                stored_edit = {
                    "path": edit["path"],
                    "symbol": edit["symbol"],
                    "signature": edit["signature"],
                    "fragment": edit["fragment"],
                    "region": digest,
                    "file_after": sha(edit["source_after"]),
                    "backup": backup,
                }
                region = {"path": edit["path"], "id": integration_id, "fragment": edit["fragment"], "hash": digest}
            record = {
                **record,
                "files": {rel: sha(content) for rel, content in files.items()},
                "edit": stored_edit,
            }
            manifest["integrations"][integration_id] = record
            self._transact(writes, manifest, region, modes)
            return record

    @staticmethod
    def _check_unchanged(target, edit):
        if _read(target) != edit["source_before"]:
            raise ConflictError(f"{edit['path']} changed during integration")

    @staticmethod
    def _compiles(source):
        try:
            compile(source, "<source>", "exec", dont_inherit=True)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return False
        return True

    def _validate(self, files, edit):
        try:
            if edit:
                ast.parse(edit["source_after"])
                if self._compiles(edit["source_before"]):
                    compile(edit["source_after"], edit["path"], "exec", dont_inherit=True)
            for rel, content in files.items():
                compile(content, rel, "exec")
        except (SyntaxError, RecursionError, MemoryError) as exc:
            raise ValidationFailure(f"generated code does not parse: {exc}") from exc
        if edit and edit["symbol"]:
            signature = signature_of(edit["source_after"], edit["symbol"], edit.get("lineno"))
            if signature != edit["signature"]:
                raise ValidationFailure(f"the edit would change the signature of {edit['symbol']}")
        facades = [r for r in files if not r.endswith(("__init__.py", "policy.py", "remote.py"))]
        with tempfile.TemporaryDirectory() as stage:
            for rel, content in files.items():
                path = Path(stage) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            # Import from where the package sits. The folders above it need not be valid module names.
            paths = sorted({str(Path(stage) / Path(r).parent.parent) for r in facades})
            modules = [".".join(Path(r).parts[-2:])[:-3] for r in facades]
            try:
                check = subprocess.run(
                    [sys.executable, "-c", f"import importlib, sys; sys.path[:0] = {paths!r}; "
                                           f"[importlib.import_module(m) for m in {modules!r}]"],
                    capture_output=True, text=True, cwd=stage, timeout=30,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValidationFailure("generated code took too long to import") from exc
            if check.returncode:
                reason = check.stderr.strip().splitlines()[-1] if check.stderr.strip() else "unknown error"
                raise ValidationFailure(f"generated code does not import: {reason}")

    @staticmethod
    def _region_lines(source, begin, end):
        lines = source_lines(source)
        starts = [i for i, l in enumerate(lines) if l.strip() == begin]
        stops = [i for i, l in enumerate(lines) if l.strip() == end]
        if len(starts) != 1 or len(stops) != 1 or stops[0] < starts[0]:
            return None
        return lines, starts[0], stops[0]

    def _region_hash(self, source, begin, end):
        located = self._region_lines(source, begin, end)
        if not located:
            return None
        lines, start, stop = located
        # Flipping a switch is normal use of the block, so it is not a modification.
        return sha(_SWITCH_VALUE.sub(r'\1"*"', "".join(lines[start:stop + 1])))

    def block_text(self, integration_id):
        """The block exactly as it stands in the file, or the snippet if there was no file."""
        entry = self.manifest()["integrations"][integration_id]
        if not entry["edit"]:
            return "\n".join(entry.get("snippet", []))
        begin, end = markers(integration_id)
        located = self._region_lines(self.read(entry["edit"]["path"]), begin, end)
        if not located:
            return ""
        lines, start, stop = located
        return "".join(lines[start:stop + 1]).rstrip("\r\n")

    def switch_values(self, integration_id, rel):
        """The switches as they stand in the file: {variable name: "true" or "false"}."""
        begin, end = markers(integration_id)
        located = self._region_lines(self.read(rel), begin, end)
        if not located:
            return {}
        lines, start, stop = located
        found = (_SWITCH_LINE.search(line) for line in lines[start:stop + 1])
        return {m[1]: m[2].lower() for m in found if m}

    # Release ---------------------------------------------------------------
    def _changes(self, integration_id):
        """(kind, description) for each change. Kinds missing and removed leave nothing to protect."""
        entry = self.manifest()["integrations"][integration_id]
        changes = []
        for rel, digest in entry["files"].items():
            current = _digest(self.resolve(rel))
            if current is None:
                changes.append(("missing", f"{rel} is missing"))
            elif current != digest:
                changes.append(("modified", f"{rel} was modified"))
        edit = entry["edit"]
        if not edit:
            return changes
        path = self.resolve(edit["path"])
        if not path.exists():
            changes.append(("missing", f"{edit['path']} is missing"))
            return changes
        begin, end = markers(integration_id)
        try:
            current = _read(path)
        except (ValueError, OSError):
            changes.append(("unreadable", f"{edit['path']} cannot be read as text"))
            return changes
        if self._region_hash(current, begin, end) != edit["region"]:
            if begin in current or end in current:
                changes.append(("modified", f"the DCAP region in {edit['path']} was modified"))
            else:
                changes.append(("removed", f"the DCAP region in {edit['path']} was removed"))
        elif begin not in current:  # an older integration whose markers never formed a region
            changes.append(("modified", f"the DCAP region in {edit['path']} is damaged"))
        return changes

    def conflicts(self, integration_id):
        """Everything that has changed since DCAP wrote this integration."""
        return [text for _, text in self._changes(integration_id)]

    def release(self, integration_id, force=False):
        with self._exclusive():
            manifest = self.manifest()
            entry = manifest["integrations"].get(integration_id)
            if entry is None:
                raise ConflictError(f"unknown integration {integration_id}")
            changes = self._changes(integration_id)
            problems = [text for _, text in changes]
            blocking = [text for kind, text in changes if kind not in {"missing", "removed"}]
            if blocking and not force:
                raise ConflictError("; ".join(blocking))

            edit = entry["edit"]
            writes = {}
            if edit:
                writes[edit["backup"]] = None
                writes.update(self._restored_source(edit, integration_id))
            del manifest["integrations"][integration_id]
            still_used = {r for e in manifest["integrations"].values() for r in e["files"]}
            for rel, digest in entry["files"].items():
                target = self.resolve(rel)
                # Keep anything still shared, and never delete a file someone changed.
                if rel not in still_used and _digest(target) == digest:
                    writes[rel] = None
            self._transact(writes, manifest)
            return {"released": integration_id, "kept_conflicts": problems}

    def _restored_source(self, edit, integration_id):
        """What the edited file should become. Refuses if DCAP's code could not be taken out of it."""
        path = self.resolve(edit["path"])
        if not path.is_file():
            return {}
        try:
            current = _read(path)
        except (ValueError, OSError) as exc:
            raise ConflictError(f"{edit['path']} cannot be read as text, so nothing was released") from exc
        if sha(current) == edit["file_after"]:
            return {edit["path"]: self.read(edit["backup"])}
        begin, end = markers(integration_id)
        if begin not in current and end not in current:
            return {}  # the region is already gone
        try:
            restored = self._remove_region(current, integration_id, edit["fragment"])
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            raise ConflictError(f"{edit['path']} does not parse at the moment, so nothing was released") from exc
        if restored is None:
            raise ConflictError(f"the DCAP markers in {edit['path']} are damaged, so nothing was released")
        return {edit["path"]: restored}

    def _remove_region(self, source, integration_id, fragment):
        begin, end = markers(integration_id)
        located = self._region_lines(source, begin, end)
        if not located:
            return None
        lines, start, stop = located
        indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
        newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
        restored = [f"{indent}{text}{newline}" for text in fragment]
        result = "".join([*lines[:start], *restored, *lines[stop + 1:]])
        ast.parse(result)
        return result
