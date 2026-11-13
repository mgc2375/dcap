"""DCAP listens to what the AI does natively, raises a hand, and acts only on a yes."""
import json
import os
from pathlib import Path
import re

from .bridges import name_tokens

_WANT = re.compile(
    r"\b(?:i need to|we need to|need to|i need|we need|i want to|we want to|want to|i want|we want|i'?d like to|"
    r"i have to|we have to|have to|let me|let's|i'?ll|we'?ll|i will|we will|i'?m going to|we'?re going to|"
    r"going to|looking for|how (?:do|can) (?:i|we))\b",
    re.IGNORECASE,
)
_FILLER = re.compile(r"\b(?:add|tool|tools|implement|function|helper|script|code|new|some|that|which|can|in)\b", re.IGNORECASE)
_YES = re.compile(r"^\W*(?:yes|yeah|yep|sure|ok|okay|please)\b", re.IGNORECASE)
_NO = re.compile(r"^\W*(?:no|nope|skip|not now)\b", re.IGNORECASE)
_NAMES = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)|#\s*(.+)$", re.MULTILINE)
# Words that mean the AI is working on its own code, not looking for a capability.
_OWN_WORK = {"fix", "bug", "test", "refactor", "rename", "parse", "parsing", "lint", "typo", "comment", "doc", "format", "error"}
_MAX_IGNORED = 2


class Listener:
    def __init__(self, dcap):
        self.dcap = dcap
        self.path = dcap.workspace.state / "listener.json"
        self.state = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return {"pending": None, "declined": [], "seen": 0, **(data if isinstance(data, dict) else {})}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state))
        os.replace(temp, self.path)

    # What the AI is doing -----------------------------------------------------
    @staticmethod
    def _from_speech(text):
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text.replace("\u2019", "'")):
            if _WANT.search(sentence):
                yield _FILLER.sub(" ", _WANT.sub(" ", sentence)).strip()

    def _from_code(self, code):
        for match in _NAMES.finditer(code):
            if match.group(1):
                yield " ".join(sorted(name_tokens(match.group(1), self.dcap.resolver.lex)))
            elif match.group(2):
                yield _FILLER.sub(" ", match.group(2)).strip()

    def _subject(self, candidate, from_code):
        resolver = self.dcap.resolver
        if not candidate or resolver.lex.tokens(candidate) & _OWN_WORK:
            return None
        resolution = resolver.resolve(candidate, require_params=False)
        if resolution.mode == "goal":
            return resolution.goal, self.dcap.registries.goals[resolution.goal].name, candidate
        if not from_code:
            # People describe a need inside a longer sentence, so look for it within the sentence.
            found = self._within_sentence(resolver.lex.tokens(candidate))
            if found:
                return found
        # A broad need is only clear enough when the AI says it. A word in code is not a need.
        if resolution.mode == "direct" and not from_code:
            titles = [self.dcap.registries.classes[c].title for c in resolution.classes]
            return "tools." + ".".join(resolution.classes), " and ".join(titles) + " capabilities", candidate
        return None

    def _within_sentence(self, tokens):
        """A goal's whole wording, or at least two of a class's own words, within one sentence."""
        registries, resolver = self.dcap.registries, self.dcap.resolver
        for goal_id, aliases in sorted(resolver.goal_aliases.items()):
            if any(alias and alias <= tokens for alias in aliases):
                return goal_id, registries.goals[goal_id].name, goal_id
        for cls in sorted(registries.classes.values(), key=lambda c: c.name):
            if len(tokens & resolver.lex.tokens(" ".join(cls.keywords))) >= 2:
                return f"tools.{cls.name}", f"{cls.title} capabilities", f"help with {cls.keywords[0]}"
        return None

    def _delivered(self, subject):
        existing = self.dcap.workspace.manifest()["integrations"].values()
        return any(e["subject"] == subject or e["subject"].startswith(subject + "_") for e in existing)

    def notice(self, said=(), wrote=()):
        """Raise a hand if there is something to offer. Returns the offer, or None."""
        if self.state["pending"]:
            return None
        spoken = [(c, False) for text in said for c in self._from_speech(text)]
        written = [(c, True) for text in wrote for c in self._from_code(text)]
        for candidate, from_code in [*spoken, *written]:
            found = self._subject(candidate, from_code)
            if not found:
                continue
            subject, label, request = found
            if subject in self.state["declined"] or self._delivered(subject):
                continue
            self.state["pending"] = {"request": request, "subject": subject, "ignored": 0}
            return f"DCAP: want help with {label.lower()}? Yes or no."
        return None

    def answer(self, said=()):
        """Read the AI's reply to an offer. Returns what was pasted on a yes, otherwise None."""
        pending = self.state["pending"]
        if not pending or not said:
            return None
        reply = said[0]
        if _YES.search(reply):
            self.state["pending"] = None
            return self.dcap.door(pending["request"]) or None
        if _NO.search(reply):
            self.state["pending"] = None
            self.state["declined"].append(pending["subject"])
            return None
        pending["ignored"] += 1
        if pending["ignored"] >= _MAX_IGNORED:  # carrying on without answering is a no
            self.state["pending"] = None
            self.state["declined"].append(pending["subject"])
        return None

    def observe(self, said=(), wrote=()):
        """One step of listening. Returns the text to show the AI, or None."""
        had_offer = bool(self.state["pending"])
        message = self.answer(said) if had_offer else None
        # A reply to an offer is about that offer, so it isn't also read as a new need.
        return message or self.notice(() if had_offer else said, wrote)


def new_speech(transcript, listener):
    """Assistant text added to a Claude Code transcript since the last look."""
    said = []
    try:
        lines = Path(transcript).read_text(encoding="utf-8").splitlines()
    except (OSError, TypeError, ValueError):
        return said
    for line in lines[listener.state["seen"]:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            said.append(content)
        elif isinstance(content, list):
            said += [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    listener.state["seen"] = len(lines)
    return said


def claude_code_hook(dcap, event):
    """Adapter for Claude Code hooks. Returns the JSON to print, or None to stay silent."""
    listener = Listener(dcap)
    said = new_speech(event.get("transcript_path"), listener)
    name = event.get("hook_event_name")
    wrote = []
    if name == "PostToolUse" and event.get("tool_name") in {"Write", "Edit", "MultiEdit"}:
        tool_input = event.get("tool_input") or {}
        wrote = [tool_input[key] for key in ("content", "new_string") if isinstance(tool_input.get(key), str)]
        wrote += [e["new_string"] for e in tool_input.get("edits", []) if isinstance(e, dict) and isinstance(e.get("new_string"), str)]
    message = listener.observe(said, wrote)
    listener.save()
    if not message:
        return None
    if name == "Stop":
        return {"decision": "block", "reason": message}
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": message}}
