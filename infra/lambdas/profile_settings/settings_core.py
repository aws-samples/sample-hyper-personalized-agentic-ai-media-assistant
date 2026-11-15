"""Pure Settings API logic (docs/unified-profile-design.md §12): no AWS calls.

L3 (`L3#DECLARED`) holds the viewer's own rules. This module is the single
definition of which fields a viewer may set, what values are allowed, and how
a stored item maps to the API shape. The Lambda is the only writer of L3 after
a household is seeded.

Validation policy:
- closed vocabularies only where the data is closed: genres come from the
  static genre list (`genres.json`, mirroring the catalog's genre values),
  languages from ISO 639-1, consent flags are booleans;
- open concepts stay open text: `avoid_themes` and the names in `followed`
  are free text (bounded, trimmed, de-duplicated), matched by meaning by the
  consumers. Followed entities carry an optional canonical `id` set by the
  entity resolver; the raw `name` is always kept.
"""

import re
import unicodedata

DECLARED_SK = "L3#DECLARED"
SOURCES = {"onboarding", "settings"}          # who is asking; recorded as `updated_by`

SUBTITLES = {"off", "on"}
FOLLOWED_KINDS = ("teams", "players", "drivers", "talent")
CONSENT_KEYS = ("personalization_from_history", "conversation_memory")

MAX_AVOID_THEMES = 10
MAX_TEXT_LEN = 80
MAX_FOLLOWED_PER_KIND = 25
MAX_LANGUAGES = 10
MIN_LENGTH_MIN, MAX_LENGTH_MIN = 30, 600

_ENTITY_ID = re.compile(r"^[a-z0-9][a-z0-9:_.\-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

ISO_639_1 = frozenset("""
aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy
da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu
hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb
lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om
or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw
ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu
""".split())

EDITABLE = ("excluded_genres", "avoid_themes", "kids_profile", "languages",
            "subtitles", "max_length_minutes", "followed", "consent")
META = ("version", "updated_at", "updated_by", "last_request_id")


def defaults():
    """What a member without an L3 item gets: no rules, history-based personalization on."""
    return {"excluded_genres": [], "avoid_themes": [], "kids_profile": False,
            "languages": [], "subtitles": "off", "max_length_minutes": None,
            "followed": {k: [] for k in FOLLOWED_KINDS},
            "consent": {k: True for k in CONSENT_KEYS}}


class Invalid(ValueError):
    def __init__(self, details):
        super().__init__("; ".join(f"{d['field']}: {d['reason']}" for d in details))
        self.details = details


def clean_text(value):
    """Trimmed, whitespace-collapsed, NFC text without control characters, or None."""
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        return None
    value = " ".join(value.split())
    return value if 0 < len(value) <= MAX_TEXT_LEN else None


def _dedupe(items, key):
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def validate(declared, genre_vocab):
    """Validate a full set of editable fields; return the normalized dict or raise Invalid.

    `genre_vocab` is the allowed genre ids (lowercase, from `genres.json`). Every editable
    field must be present: PUT replaces the whole rule set, so a client that
    forgets a field cannot silently reset it (consent especially).
    """
    errs = []

    def err(field, reason):
        errs.append({"field": field, "reason": reason})

    if not isinstance(declared, dict):
        raise Invalid([{"field": "declared", "reason": "must be an object"}])
    unknown = sorted(set(declared) - set(EDITABLE))
    if unknown:
        err("declared", f"unknown fields: {unknown}")
    missing = [f for f in EDITABLE if f not in declared]
    if missing:
        err("declared", f"missing fields: {missing}")
    if errs:
        raise Invalid(errs)

    out = {}
    genres = declared["excluded_genres"]
    if not isinstance(genres, list) or any(not isinstance(g, str) for g in genres):
        err("excluded_genres", "must be a list of genre names")
    else:
        norm = _dedupe([g.strip().lower() for g in genres], key=lambda g: g)
        bad = [g for g in norm if g not in genre_vocab]
        if bad:
            err("excluded_genres", f"not catalog genres: {bad}")
        out["excluded_genres"] = norm

    themes = declared["avoid_themes"]
    if not isinstance(themes, list):
        err("avoid_themes", "must be a list of text")
    else:
        cleaned = [clean_text(t) for t in themes]
        if any(c is None for c in cleaned):
            err("avoid_themes", f"each theme must be 1-{MAX_TEXT_LEN} characters of plain text")
        cleaned = _dedupe([c for c in cleaned if c], key=str.casefold)
        if len(cleaned) > MAX_AVOID_THEMES:
            err("avoid_themes", f"at most {MAX_AVOID_THEMES}")
        out["avoid_themes"] = cleaned

    if not isinstance(declared["kids_profile"], bool):
        err("kids_profile", "must be true or false")
    out["kids_profile"] = declared["kids_profile"] is True

    langs = declared["languages"]
    if not isinstance(langs, list) or any(not isinstance(x, str) or x.strip().lower() not in ISO_639_1 for x in langs):
        err("languages", "must be a list of ISO 639-1 codes")
    else:
        norm = _dedupe([x.strip().lower() for x in langs], key=lambda x: x)
        if len(norm) > MAX_LANGUAGES:
            err("languages", f"at most {MAX_LANGUAGES}")
        out["languages"] = norm

    if declared["subtitles"] not in SUBTITLES:
        err("subtitles", f"must be one of {sorted(SUBTITLES)}")
    out["subtitles"] = declared["subtitles"]

    ml = declared["max_length_minutes"]
    if ml is not None and (isinstance(ml, bool) or not isinstance(ml, int) or not MIN_LENGTH_MIN <= ml <= MAX_LENGTH_MIN):
        err("max_length_minutes", f"must be null or a whole number {MIN_LENGTH_MIN}-{MAX_LENGTH_MIN}")
    out["max_length_minutes"] = ml

    out["followed"] = _validate_followed(declared["followed"], err)

    consent = declared["consent"]
    if not isinstance(consent, dict) or set(consent) != set(CONSENT_KEYS) \
            or any(not isinstance(v, bool) for v in consent.values()):
        err("consent", f"must set {list(CONSENT_KEYS)} to true or false")
    else:
        out["consent"] = dict(consent)

    if errs:
        raise Invalid(errs)
    return out


def _validate_followed(followed, err):
    out = {k: [] for k in FOLLOWED_KINDS}
    if not isinstance(followed, dict) or set(followed) - set(FOLLOWED_KINDS):
        err("followed", f"must be an object with keys {list(FOLLOWED_KINDS)}")
        return out
    for kind in FOLLOWED_KINDS:
        entries = followed.get(kind, [])
        if not isinstance(entries, list):
            err(f"followed.{kind}", "must be a list")
            continue
        rows = []
        for e in entries:
            if isinstance(e, str):          # bare text: not resolved yet
                e = {"name": e}
            if not isinstance(e, dict) or set(e) - {"name", "id"}:
                err(f"followed.{kind}", "entries are {name, id?}")
                continue
            name = clean_text(e.get("name"))
            if name is None:
                err(f"followed.{kind}", f"name must be 1-{MAX_TEXT_LEN} characters of plain text")
                continue
            row = {"name": name}
            if e.get("id") is not None:
                if not isinstance(e["id"], str) or not _ENTITY_ID.match(e["id"]):
                    err(f"followed.{kind}", f"invalid id for {name!r}")
                    continue
                row["id"] = e["id"]
            rows.append(row)
        # The same entity once: by canonical id when resolved, else by name.
        rows = _dedupe(rows, key=lambda r: r.get("id") or "name:" + r["name"].casefold())
        if len(rows) > MAX_FOLLOWED_PER_KIND:
            err(f"followed.{kind}", f"at most {MAX_FOLLOWED_PER_KIND}")
        out[kind] = rows
    return out


def parse_put(body):
    """Request body -> (expected_version, source, request_id, declared) or raise Invalid."""
    if not isinstance(body, dict):
        raise Invalid([{"field": "body", "reason": "must be a JSON object"}])
    errs = []
    unknown = sorted(set(body) - {"version", "source", "request_id", "declared", "loved_genres"})
    if unknown:
        errs.append({"field": "body", "reason": f"unknown fields: {unknown}"})
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        errs.append({"field": "version", "reason": "the version you read (0 when none exists yet)"})
    source = body.get("source", "settings")
    if source not in SOURCES:
        errs.append({"field": "source", "reason": f"must be one of {sorted(SOURCES)}"})
    request_id = body.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not _REQUEST_ID.match(request_id)):
        errs.append({"field": "request_id", "reason": "8-64 characters of [A-Za-z0-9_-]"})
    if errs:
        raise Invalid(errs)
    return version, source, request_id, body.get("declared")


def parse_loved(body, genre_vocab, excluded):
    """`loved_genres` from a PUT body -> lowercase genre ids, or raise Invalid.

    Likes are not rules, so they are never stored in L3: the Settings API hands
    them to the stream processor, which gives L2 a weak, fading start for each
    (the processor stays the only L2 writer). A genre cannot be both loved and
    excluded in one request.
    """
    loved = body.get("loved_genres", [])
    if not isinstance(loved, list) or not all(isinstance(g, str) for g in loved):
        raise Invalid([{"field": "loved_genres", "reason": "must be a list of genre names"}])
    norm = _dedupe([g.strip().lower() for g in loved], key=lambda g: g)
    bad = [g for g in norm if g not in genre_vocab]
    if bad:
        raise Invalid([{"field": "loved_genres", "reason": f"not catalog genres: {bad}"}])
    both = [g for g in norm if g in set(excluded)]
    if both:
        raise Invalid([{"field": "loved_genres", "reason": f"also excluded: {both}"}])
    return norm


def from_item(item):
    """Stored L3 item (or None) -> API view: editable fields over defaults, plus metadata."""
    item = item or {}
    base = defaults()
    declared = {k: item.get(k, base[k]) for k in EDITABLE}
    # Older items may hold followed entries as bare strings.
    followed = declared.get("followed") or {}
    declared["followed"] = {k: [e if isinstance(e, dict) else {"name": e} for e in followed.get(k) or []]
                            for k in FOLLOWED_KINDS}
    declared["consent"] = {**base["consent"], **(declared.get("consent") or {})}
    return {"version": int(item.get("version") or 0), "updated_at": item.get("updated_at"),
            "updated_by": item.get("updated_by"), "declared": declared}


def changed_fields(old_view, new_declared):
    """Names (never values) of the fields this write changes."""
    old = old_view["declared"]
    return [k for k in EDITABLE if old.get(k) != new_declared.get(k)]


def build_item(profile_id, existing, new_declared, version, source, request_id, stamp):
    """The item to write: other stored attributes kept, editable fields replaced."""
    keep = {k: v for k, v in (existing or {}).items() if k not in EDITABLE and k not in META
            and k not in ("user_id", "data_type")}
    item = {**keep, "user_id": profile_id, "data_type": DECLARED_SK, **new_declared,
            "version": version, "updated_at": stamp, "updated_by": source}
    if request_id:
        item["last_request_id"] = request_id
    return item


def is_retry_of_last_write(existing, expected_version, request_id):
    """A PUT whose earlier attempt succeeded (the response was lost) carries the
    same request_id against the version just before the stored one."""
    return bool(request_id and existing and existing.get("last_request_id") == request_id
                and int(existing.get("version") or 0) == expected_version + 1)
