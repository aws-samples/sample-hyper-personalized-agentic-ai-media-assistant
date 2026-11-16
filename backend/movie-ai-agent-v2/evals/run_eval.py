"""Understanding-step eval: scores understand() ("cand") against the case suite.

Live Bedrock calls (Haiku 4.5). Run from this directory:
    AWS_REGION=us-west-2 V2_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
    python run_eval.py .. 2 all            # 215 cases x 2 runs, ~5 min
    python run_override.py ..              # L3 override suite, 38 cases x 3 runs
Cases: cases.py (CASES + HELDOUT) and gen/ (written independently; gen/fixes.py holds
reviewed label corrections). Never copy these into fewshot.py.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])  # agent source dir
from understand import understand  # noqa: E402
import cases as _c
from cases import STEMS, GENRE_OF, PERSONA_GENRES  # noqa: E402

REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
CASES = _c.HELDOUT if len(sys.argv) > 3 and sys.argv[3] == 'heldout' else _c.CASES
if len(sys.argv) > 3 and sys.argv[3] == "all":
    import glob, importlib.util
    CASES = [dict(c, fam="mine") for c in _c.CASES + _c.HELDOUT]
    for path in sorted(glob.glob("gen/cases_*.py")):
        spec = importlib.util.spec_from_file_location(path, path)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        fam = path.split("cases_")[1][:-3]
        from gen.fixes import FIXES, normalize
        CASES += [normalize(dict(c, fam=fam, **FIXES.get((fam, i), {}))) for i, c in enumerate(m.CASES)]
        STEMS.update(getattr(m, "EXTRA_STEMS", {}))
        GENRE_OF.update(getattr(m, "EXTRA_GENRE_OF", {}))
    # Scorer stem gaps found in review (synonyms the model legitimately used).
    STEMS["raunchy"] = STEMS["raunchy"] + ["swear", "gross", "profan", "language"]
    if "truestory" in STEMS:
        STEMS["truestory"] = STEMS["truestory"] + ["real ", "true crime", "biograph"]
OK_GENRES = {"something light tonight, nothing too violent, maybe from the last few years": {"comedy"}}


def has(text: str, concept: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in STEMS.get(concept, [concept]))


def candidate(c):
    t0 = time.perf_counter()
    import os
    rules = {"A": ["avoid genre: musical", "avoid theme: excessive gore", "avoid theme: animal cruelty"],
             "R": ["avoid genre: horror", "avoid theme: gore"]}[c["who"]] if os.environ.get("WITH_RULES", "1") == "1" else None
    d = understand(c["msg"], c.get("hist", []), [], rules=rules)
    d["_s"] = round(time.perf_counter() - t0, 2)
    d["_avoid_text"] = " ".join(d["genres_exclude"] + d["avoid_soft"] + d["other_constraints"] + d.get("themes_exclude", []))
    d["_soft_text"] = " ".join(d["avoid_soft"] + d.get("themes_exclude", []))
    d["_want_text"] = " ".join(d["genres_include"] + d["keywords"] + d["other_constraints"]) + " " + d["search_text"] + " " + d["mood"] + " " + str(d.get("person") or "") + " " + str(d.get("exact_title") or "") + " " + d["category"]
    d["_query"] = d["search_text"]
    d["_neg"] = d["knn_text"]
    return d


def score(c, d):
    fails = []
    if d.get("intent") == "answer":  # every suite case asks for titles
        fails.append("wrong-intent:answer")
    if d["path"] not in c["paths"]:
        fails.append(f"route={d['path']}")
    for a in c.get("avoid", []):
        if not has(d["_avoid_text"], a):
            fails.append(f"lost-avoid:{a}")
    for a in c.get("hard", []):
        if GENRE_OF.get(a, a) not in d["genres_exclude"]:
            fails.append(f"not-hard:{a}")
    for a in c.get("soft", []):
        if GENRE_OF.get(a, a) in d["genres_exclude"]:
            fails.append(f"over-hard:{a}")
    for a in c.get("notavoid", []):
        if GENRE_OF.get(a, a) in d["genres_exclude"] or has(d["_soft_text"], a):
            fails.append(f"kept-dropped-avoid:{a}")
    for w in c.get("want", []):
        if not has(d["_want_text"], w):
            fails.append(f"lost-want:{w}")
    for w in c.get("notwant", []):
        if GENRE_OF.get(w, w) in d["genres_include"]:
            fails.append(f"wrong-want:{w}")
    if d.get("_neg") is not None and re.search(r"\b(without|not|no|less|non|minimal|isn't|free of)\b", d["_neg"].lower()):
        fails.append("neg-in-search")
    for k, v in (c.get("fields") or {}).items():
        got = d.get(k)
        if k in ("person", "exact_title"):
            txt = " ".join(got) if isinstance(got, list) else str(got or "")
            ok = str(v).lower() in txt.lower()
        elif k in ("min_year", "max_year"):
            ok = got is not None and abs(int(got) - int(v)) <= (0 if k == "max_year" or v % 10 == 0 else 1)
        elif k == "min_rating":
            ok = got is not None and abs(float(got) - float(v)) < 0.51
        else:
            ok = got == v
        if not ok:
            fails.append(f"field:{k}={got}")
    if c.get("notperson") and c["notperson"] in str(d.get("person") or "").lower():
        fails.append("field:person-filter-on-excluded-person")
    if c.get("leak"):
        added = set(d["genres_include"]) & set(PERSONA_GENRES[c["who"]]) - OK_GENRES.get(c["msg"], set())
        if added:
            fails.append(f"pref-leak:{','.join(sorted(added))}")
    return fails


def run_one(args):
    i, rep, kind = args
    c = CASES[i]
    try:
        d = candidate(c)
    except Exception as e:  # noqa: BLE001
        return i, rep, kind, {"path": "error", "_s": 0, "_query": str(e)[:120]}, ["error"]
    return i, rep, kind, d, score(c, d)


KINDS = ("cand",)
jobs = [(i, r, k) for i in range(len(CASES)) for r in range(REPS) for k in KINDS]
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(run_one, jobs))

out = {}
for i, rep, kind, d, fails in res:
    out.setdefault(i, {}).setdefault(kind, []).append(
        {"fails": fails, "path": d.get("path"), "s": d.get("_s"), "query": d.get("_query"),
         "excl": d.get("genres_exclude"), "soft": d.get("avoid_soft"), "incl": d.get("genres_include"),
         "open": d.get("open_ended"), "ok": d.get("_ok", True), "other": d.get("other_constraints"), "llm": d.get("llm_text")})
json.dump({str(k): v for k, v in out.items()}, open(sys.argv[4] if len(sys.argv) > 4 else "results.json", "w"), indent=1)

for kind in KINDS:
    runs = [r for i in out for r in out[i][kind]]
    passed = sum(not r["fails"] for r in runs)
    cats = {}
    for r in runs:
        for f in r["fails"]:
            k = f.split(":")[0].split("=")[0]
            cats[k] = cats.get(k, 0) + 1
    lat = sorted(r["s"] for r in runs)
    stable = sum(len({json.dumps([r["path"], r["excl"], r["soft"], r["incl"]]) for r in out[i][kind]}) == 1 for i in out)
    print(f"{kind}: pass {passed}/{len(runs)}  fails={cats}  p50={lat[len(lat)//2]}s p90={lat[int(len(lat)*.9)]}s  "
          f"stable={stable}/{len(out)}  json_fail={sum(not r['ok'] for r in runs)}")
print()
fams = sorted({c.get("fam", "mine") for c in CASES})
for fam in fams:
    idx = [i for i in out if CASES[i].get("fam", "mine") == fam]
    row = []
    for kind in KINDS:
        runs = [r for i in idx for r in out[i][kind]]
        row.append(f"{kind} {sum(not r['fails'] for r in runs)}/{len(runs)}")
    print(f"family {fam:10} cases={len(idx):3}  " + "  ".join(row))
print()
for i in sorted(out):
    if any(r["fails"] for r in out[i]["cand"]):
        print(f"[{i}] {CASES[i]['msg']!r}" + (f" (after {CASES[i]['hist']})" if CASES[i].get('hist') else ""))
        for kind in KINDS:
            for r in out[i][kind]:
                print(f"   {kind}: {r['path']:9} fails={r['fails']} excl={r['excl']} soft={r['soft']} incl={r['incl']} q={r['query']!r}")
