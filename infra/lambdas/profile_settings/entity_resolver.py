"""Followed-entity resolver, tier 1: free text -> canonical sports entity id.

A viewer types who they follow ("Asante Brok", "hcf", "redclife"). This maps
that text onto the sports entity registry (`sports_entities.json`) by data,
not by hand-written word lists: exact match on a normalized name or alias
first, then a bounded edit distance for typos. The answer is accepted only
when exactly one entity is the closest, so an ambiguous name stays
unresolved instead of being guessed. Unresolved text is kept as typed and
has no effect on sports screens.

The resolver also puts each match under the right kind: a player name typed
under teams is stored as a player. Only `teams`, `players` and `drivers` are
sports kinds; `talent` is left alone.

Production would back this with a search index fed by a sports data provider
(fuzzy type-ahead, multilingual aliases) and a cached model fallback for text
the index cannot place. The interface is the same: `resolve(name) -> entity`.
"""

import json
import os
import re
import unicodedata

REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_entities.json")
SPORTS_KINDS = ("teams", "players", "drivers")
KIND_BY_TYPE = {"team": "teams", "player": "players", "driver": "drivers"}
_PUNCT = re.compile(r"[^\w\s-]")


def normalize(text):
    """Casefolded, accent-free, punctuation-free, single-spaced text."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text.casefold()).replace("-", " ")
    return " ".join(text.split())


def max_typos(text):
    """Edits allowed for a typo match: none for short text, so 'max' never becomes 'mac'."""
    n = len(text)
    return 0 if n <= 4 else 1 if n <= 8 else 2


def distance(a, b, limit):
    """Optimal-string-alignment edit distance, or limit + 1 once it is exceeded."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


class Registry:
    def __init__(self, entities):
        self.by_id = {e["id"]: e for e in entities}
        keys = {}          # normalized spelling -> set of entity ids
        for e in entities:
            for spelling in self._spellings(e):
                keys.setdefault(spelling, set()).add(e["id"])
        # A generated spelling shared by two entities identifies neither.
        self.keys = {k: next(iter(ids)) for k, ids in keys.items() if len(ids) == 1}

    @staticmethod
    def _spellings(e):
        names = [e["name"], *e.get("aliases", [])]
        if e["type"] in ("player", "driver"):
            parts = normalize(e["name"]).split()
            if len(parts) > 1:
                names += [parts[-1], f"{parts[0][0]} {parts[-1]}"]   # "lark", "t lark"
        return {normalize(n) for n in names if normalize(n)}

    @classmethod
    def load(cls, path=REGISTRY_FILE):
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f)["entities"])

    def resolve(self, text):
        """The single closest entity for free text, or None."""
        q = normalize(text)
        if not q:
            return None
        if q in self.keys:
            return self.by_id[self.keys[q]]
        limit = max_typos(q)
        if not limit:
            return None
        best, best_d = set(), limit + 1
        for key, eid in self.keys.items():
            d = distance(q, key, limit)
            if d < best_d:
                best, best_d = {eid}, d
            elif d == best_d and d <= limit:
                best.add(eid)
        return self.by_id[best.pop()] if len(best) == 1 and best_d <= limit else None

    def suggest(self, text, kind=None, limit=8):
        """Type-ahead: entities whose name or an alias starts with the text, then
        typo matches. Unlike resolve(), several candidates are fine here: the
        viewer picks one. `kind` ("teams" / "players" / "drivers") puts that kind
        first without hiding the others, since a name typed under the wrong kind
        is still the right entity."""
        q = normalize(text)
        if not q:
            return []
        score = {}         # entity id -> (rank, spelling length); lower is better

        def offer(eid, rank, key):
            cur = score.get(eid)
            if cur is None or (rank, len(key)) < cur:
                score[eid] = (rank, len(key))

        limit_typos = max_typos(q)
        for e in self.by_id.values():
            for key in self._spellings(e):
                if key == q:
                    offer(e["id"], 0, key)
                elif key.startswith(q):
                    offer(e["id"], 1, key)
                elif any(word.startswith(q) for word in key.split()):
                    offer(e["id"], 2, key)
                elif limit_typos and distance(q, key[:len(q) + limit_typos] if len(key) > len(q) else key,
                                              limit_typos) <= limit_typos:
                    offer(e["id"], 3, key)
        ordered = sorted(score, key=lambda i: (KIND_BY_TYPE[self.by_id[i]["type"]] != kind, score[i],
                                               self.by_id[i]["name"]))
        return [self.by_id[i] for i in ordered[:limit]]

    def resolve_followed(self, followed):
        """Attach canonical ids to `followed` and file each match under its kind.

        An id the client sent is kept only when the registry knows it, so a
        client cannot attach an arbitrary id to a name. Matched entries carry
        the canonical name; unmatched ones keep the text as typed, without id.
        """
        out = {k: list(v) for k, v in followed.items()}
        for kind in SPORTS_KINDS:
            out[kind] = []
        placed = set()
        for kind in SPORTS_KINDS:
            for e in followed.get(kind) or []:
                ent = self.by_id.get(e.get("id")) or self.resolve(e["name"])
                if ent is None:
                    row, target, key = {"name": e["name"]}, kind, "name:" + normalize(e["name"])
                else:
                    row, target, key = {"name": ent["name"], "id": ent["id"]}, KIND_BY_TYPE[ent["type"]], ent["id"]
                if key not in placed:
                    placed.add(key)
                    out[target].append(row)
        return out
