"""Deterministic request interpretation: orchestrate only when clear, otherwise expose."""
from dataclasses import dataclass, field
import ipaddress
import re

ORCHESTRATE_AT = 0.75
MIN_MARGIN = 0.15
SUGGEST_AT = 0.6
_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?"
# Whole mentions only: part of 10.0.0.1.5, 10.0.0.1:8080 or a URL is not an address.
_ADDRESS = re.compile(rf"(?<![\w.:/@-])(?:{_IPV4}|(?:[a-z0-9-]+\.)+[a-z]{{2,}})(?![\w-]|[.:/]\w)")
# A name like main.py is a file. A real host in one of these domains can be passed as a parameter.
_FILE_SUFFIXES = {
    "py", "pyi", "ts", "tsx", "js", "jsx", "go", "rs", "java", "rb", "php", "cs", "cpp", "cc", "kt", "swift",
    "sh", "json", "yaml", "yml", "toml", "txt", "md", "cfg", "conf", "config", "ini", "html", "css", "csv", "log",
    "xml", "sql", "lock",
}


@dataclass
class Resolution:
    mode: str  # goal | direct | unresolved
    goal: str | None = None
    params: dict = field(default_factory=dict)
    classes: list = field(default_factory=list)
    tokens: set = field(default_factory=set)
    score: float = 0.0
    suggestion: dict | None = None
    reason: str = ""
    ranked: list = field(default_factory=list)


class Lexicon:
    def __init__(self, vocabulary, reserved=()):
        self.stopwords = set(vocabulary.get("stopwords", []))
        self.vague = set(vocabulary.get("vague_cues", []))
        self.phrases = sorted(vocabulary.get("target_phrases", []), key=len, reverse=True)
        self.reserved = {name.lower() for name in reserved}  # goal IDs look like host names
        self.canonical = {}
        for root, words in vocabulary.get("synonyms", {}).items():
            for word in words:
                self.canonical[word] = root
        self.roots = set(vocabulary.get("synonyms", {}))

    @staticmethod
    def _singular(word):
        if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            return word[:-1]
        return word

    def tokens(self, text):
        out = set()
        for raw in re.findall(r"[^\W_]+", text.lower()):
            if raw in self.stopwords or raw.isdigit():
                continue
            word = self.canonical.get(raw) or self._singular(raw)
            out.add(self.canonical.get(word, word))
        return out

    def _is_target(self, candidate):
        if re.fullmatch(_IPV4, candidate):
            try:
                ipaddress.ip_network(candidate, strict=False)
            except ValueError:
                return False
            return True
        return candidate not in self.reserved and candidate.rsplit(".", 1)[-1] not in _FILE_SUFFIXES

    def strip_addresses(self, text):
        return _ADDRESS.sub(lambda m: " " if self._is_target(m.group()) else m.group(), text.lower())

    def _phrase_pattern(self, phrase):
        return rf"(?<![\w.-]){re.escape(phrase)}(?![\w-]|\.\w)"

    def strip_target(self, text):
        lowered = text.lower()
        for phrase in self.phrases:
            lowered = re.sub(self._phrase_pattern(phrase), " ", lowered)
        lowered = re.sub(r"\ba target\b|\bthe target\b", " ", lowered)
        return self.strip_addresses(lowered)

    def target_mentions(self, text):
        """Every distinct target the text names, in order."""
        lowered = text.lower()
        found = [m.group() for m in _ADDRESS.finditer(lowered) if self._is_target(m.group())]
        if any(re.search(self._phrase_pattern(phrase), lowered) for phrase in self.phrases):
            found.append("127.0.0.1")
        return list(dict.fromkeys(found))

    def extract_target(self, text):
        """The one target the text names. More than one is not a choice DCAP can make."""
        mentions = self.target_mentions(text)
        return mentions[0] if len(mentions) == 1 else None


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


class Resolver:
    def __init__(self, registries):
        self.reg = registries
        self.lex = Lexicon(registries.vocabulary, registries.goals)
        self.goal_aliases = {
            goal.id: [self.lex.tokens(self.lex.strip_target(alias)) for alias in goal.aliases]
            for goal in registries.goals.values()
        }

    def _rank(self, tokens):
        ranked = [
            (max(1.0 if tokens == alias else _dice(tokens, alias) for alias in aliases), goal_id)
            for goal_id, aliases in self.goal_aliases.items()
        ]
        return sorted(ranked, key=lambda item: (-item[0], item[1]))

    def _classes(self, tokens):
        scored = []
        for cls in self.reg.classes.values():
            keywords = self.lex.tokens(" ".join(cls.keywords))
            hits = len(tokens & keywords)
            if hits:
                scored.append((hits, cls.name))
        if not scored:
            return []
        top = max(hits for hits, _ in scored)
        return sorted(name for hits, name in scored if hits == top)

    def _class_words(self, class_names):
        """Every word the named classes use to describe themselves."""
        words = []
        for name in class_names:
            cls = self.reg.classes[name]
            words += cls.keywords
            for core_id in cls.cores:
                words += [self.reg.cores[core_id].name, self.reg.cores[core_id].description]
        return self.lex.tokens(" ".join(words))

    def _bind(self, goal, text, params):
        """Fill parameters from the request. Returns (params, missing names)."""
        bound = dict(params)
        for name, spec in goal.parameters.items():
            if name not in bound and spec.get("extract") == "target":
                found = self.lex.extract_target(text)
                if found:
                    bound[name] = found
        missing = [n for n, s in goal.parameters.items() if s.get("required", True) and n not in bound]
        return bound, missing

    def _suggest(self, goal_ids, text, params):
        goals = []
        for goal_id in goal_ids:
            goal = self.reg.goals[goal_id]
            _, missing = self._bind(goal, text, params)
            goals.append({"goal_id": goal_id, "goal": goal.name, "missing": missing})
        return {"goals": goals} if goals else None

    def _named_goal(self, text):
        wanted = text.strip().lower()
        return next((g for g in self.reg.goals.values() if wanted in {g.id.lower(), g.name.lower()}), None)

    def resolve(self, text, params=None, require_params=True):
        """require_params=False is for delivering code, which takes its inputs when called."""
        params = dict(params or {})
        stripped = self.lex.strip_target(text)
        tokens = self.lex.tokens(stripped)
        # A host's labels say nothing about intent, so class keywords are never read from them.
        spoken = self.lex.tokens(self.lex.strip_addresses(text))
        ranked = self._rank(tokens)
        best_score, best = ranked[0] if ranked else (0.0, None)
        margin = best_score - (ranked[1][0] if len(ranked) > 1 else 0.0)
        near = [goal_id for score, goal_id in ranked[:2] if score >= SUGGEST_AT]
        vague = bool(tokens & self.lex.vague)
        classes = self._classes(tokens) or (self._classes(spoken) if vague else [])
        if classes and not vague and not near and tokens - self._class_words(classes):
            classes = []  # one shared word does not put an unrelated request in the domain

        def direct(reason, goal_ids=None):
            wanted = near if goal_ids is None else goal_ids
            merged = sorted({*classes, *(c for goal_id in wanted for c in self.reg.goals[goal_id].classes)})
            mode = "direct" if merged else "unresolved"
            return Resolution(mode, classes=merged, tokens=spoken, score=best_score, reason=reason,
                              suggestion=self._suggest(wanted, text, params), ranked=ranked)

        goal = self._named_goal(text)
        if goal:
            best_score = 1.0
        else:
            if vague:
                return direct("request is broad")
            if best is None or best_score < ORCHESTRATE_AT:
                return direct("no registered goal matches clearly")
            if margin < MIN_MARGIN:
                return direct("more than one goal matches")
            numbers = set(re.findall(r"\b\d+\b", stripped)) - set(
                re.findall(r"\b\d+\b", " ".join(self.reg.goals[best].aliases))
            )
            if numbers or tokens - set().union(*self.goal_aliases[best]):
                return direct(f"request includes something goal {best} does not cover")
            goal = self.reg.goals[best]

        bound, missing = self._bind(goal, text, params)
        if missing and require_params:
            several = len(self.lex.target_mentions(text)) > 1
            reason = "request names more than one target" if several else "goal matched but parameters are missing"
            return direct(reason, [goal.id])
        return Resolution("goal", goal=goal.id, params=bound, classes=goal.classes, tokens=spoken,
                          score=best_score, reason="clear match", ranked=ranked)

    def expose(self, class_name, tokens):
        """Core ids to expose: the whole class if small, relevant cores if large."""
        cls = self.reg.classes[class_name]
        if len(cls.cores) < cls.expose_all_below:
            return cls.cores
        relevant = [
            core_id for core_id in cls.cores
            if tokens & self.lex.tokens(f"{self.reg.cores[core_id].name} {self.reg.cores[core_id].description}")
        ]
        return relevant or cls.cores
