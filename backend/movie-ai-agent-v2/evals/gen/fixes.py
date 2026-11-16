"""Reviewer fixes to generated labels: (family, index) -> replacements. Reason per line."""
T, F, C = "taste", "fanout", "catch_all"
ANY = (T, F, C)
FIXES = {
    # 'hard' is only for catalog genres; gore/violence are qualities -> keep as avoid only.
    ("complex", 0): dict(hard=(), paths=(C,), fields={"min_year": 1990, "max_year": 1999}),  # 'someone like Nolan' is not a person filter
    ("complex", 1): dict(hard=(), paths=(F, C)),
    ("complex", 5): dict(hard=()),
    ("complex", 19): dict(hard=()),
    ("complex", 29): dict(hard=()),
    ("complex", 33): dict(hard=()),
    ("complex", 39): dict(hard=()),
    # 'like <title>' may legitimately use the title as a reference (catch_all) or go to fan-out.
    ("complex", 13): dict(paths=(F, C)),
    ("complex", 18): dict(paths=(F, C)),
    ("complex", 24): dict(paths=(F, C), fields={}),
    ("complex", 31): dict(paths=(F, C)),
    ("complex", 38): dict(paths=(F, C)),
    ("simple", 22): dict(paths=(F, C)),
    ("simple", 23): dict(paths=(F, C)),
    ("simple", 14): dict(paths=(F, C)),  # 'recent' may set a year
    # Excluding a person must not become a person FILTER (that would return only Sandler films).
    ("negation", 24): dict(paths=(F, C), fields={}, notperson="sandler"),
    ("negation", 33): dict(hard=()),  # 'rom-coms off the table' -> avoid; hard romance optional
    # Routing semantics of the existing design: kids/years are hard filters (catch_all);
    # 'like <title>' may use the title as reference (catch_all); no-filter comedy is fan-out.
    ("complex", 3): dict(paths=(F, C)),
    ("complex", 5): dict(paths=(T, C), hard=()),
    ("multiturn", 1): dict(paths=(F, C)),
    ("multiturn", 17): dict(paths=(F, C)),
    ("multiturn", 22): dict(paths=(F, C)),
    ("multiturn", 26): dict(paths=(F, C)),
    ("multiturn", 39): dict(paths=(F, C)),
    ("negation", 4): dict(paths=(F, C)),
}


def normalize(c):
    """TV requests without other filters go to fan-out over the TV shards by design."""
    if (c.get("fields") or {}).get("category") == "tv" and F not in c["paths"]:
        c = dict(c, paths=tuple(c["paths"]) + (F,))
    return c


_UNUSED = {
}
