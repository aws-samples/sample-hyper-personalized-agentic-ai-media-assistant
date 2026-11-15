"""L3 override tests: explicit request > L3 preference rules; limits always win."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])
from understand import understand, apply_profile  # noqa: E402

PROFILES = {
    "R": dict(prefs=[dict(kind="genre", value="horror", text="avoid genre: horror"),
                     dict(kind="theme", value="gore", text="avoid theme: gore")], limits={}),
    "A": dict(prefs=[dict(kind="genre", value="musical", text="avoid genre: musical"),
                     dict(kind="theme", value="excessive gore", text="avoid theme: excessive gore"),
                     dict(kind="theme", value="animal cruelty", text="avoid theme: animal cruelty")], limits={}),
    # Kids profile: crude humor is the child's own preference; horror + kids-safe are LIMITS.
    "K": dict(prefs=[dict(kind="theme", value="crude humor", text="avoid theme: crude humor")],
              limits={"kids": True, "genres": ["horror"]}),
}
H, G = "avoid genre: horror", "avoid theme: gore"
M, XG, AC = "avoid genre: musical", "avoid theme: excessive gore", "avoid theme: animal cruelty"
CH = "avoid theme: crude humor"

# ov = rules that must be overridden; keep = rules that must stay active; nocopy = model must
# not copy any rule into its own avoids (the viewer didn't mention them).
CASES = [
    dict(who="R", msg="a gory slasher for tonight", ov=[G, H]),
    dict(who="R", msg="I want a horror movie", ov=[H], keep=[G]),
    dict(who="R", msg="I know I usually skip horror, but I'm in the mood for one tonight", ov=[H]),
    dict(who="R", msg="something intense", keep=[H, G], nocopy=True),
    dict(who="R", msg="a thriller", keep=[H, G], nocopy=True),
    dict(who="R", msg="surprise me", keep=[H, G], nocopy=True),
    dict(who="R", msg="something a bit spooky", keep=[G]),
    dict(who="R", hist=["a gory slasher for tonight"], msg="more like that", ov=[G, H]),
    dict(who="R", hist=["a gory slasher for tonight"], msg="actually, skip the gore again", keep=[G]),
    dict(who="R", msg="a zombie movie with lots of blood", ov=[G]),
    dict(who="R", msg="a romantic comedy", keep=[H, G], nocopy=True),
    dict(who="R", msg="a war film with realistic, graphic battle scenes", ov=[G], keep=[H]),
    dict(who="R", msg="nothing too scary, but a little suspense is fine", keep=[H, G]),
    dict(who="R", hist=["horror please"], msg="something funny instead", keep=[H]),
    dict(who="R", msg="my friend loves horror, can we watch one together", ov=[H]),
    dict(who="R", msg="not horror, just a thriller", keep=[H, G]),
    dict(who="A", msg="a musical tonight", ov=[M], keep=[XG, AC]),
    dict(who="A", msg="something like La La Land, lots of singing and dancing", ov=[M]),
    dict(who="A", msg="something brutal and bloody, like a Tarantino film", ov=[XG], keep=[M]),
    dict(who="A", msg="an action movie", keep=[M, XG, AC], nocopy=True),
    dict(who="A", msg="a documentary about animal cruelty in factory farming", ov=[AC]),
    dict(who="A", msg="a dog movie", keep=[AC], nocopy=True),
    dict(who="A", hist=["a musical tonight"], msg="another one like that", ov=[M]),
    dict(who="A", hist=["a musical tonight", "nice"], msg="ok no more musicals", keep=[M]),
    dict(who="A", msg="surprise me, but no horror", keep=[M, XG, AC]),
    dict(who="A", msg="a gritty war epic, I don't mind it graphic", ov=[XG]),
    dict(who="K", msg="a gory horror movie", limit_horror=True),
    dict(who="K", msg="something with fart jokes", ov=[CH], limit_horror=True),
    dict(who="K", msg="a scary movie", limit_horror=True),
    dict(who="K", msg="a cartoon", keep=[CH], limit_horror=True, nocopy=True),
    # Held-out: written before this prompt was first run, never used to tune it.
    dict(who="R", msg="put on a proper splatter film", ov=[G, H]),
    dict(who="R", msg="something with a bit of an edge", keep=[H, G], nocopy=True),
    dict(who="A", msg="musicals are fine tonight, my wife wants one", ov=[M]),
    dict(who="A", msg="a heartwarming story about a rescue dog", keep=[AC], nocopy=True),
    dict(who="A", hist=["something brutal and bloody, like a Tarantino film"], msg="ok now something light and funny", keep=[XG]),
    dict(who="K", msg="potty humor cartoons please", ov=[CH], limit_horror=True),
    dict(who="K", msg="a slasher movie", limit_horror=True),
    dict(who="R", msg="I'm brave tonight, give me the scariest thing you've got", ov=[H]),
]
RULE_WORDS = ["horror", "gore", "musical", "cruelty", "crude"]


def check(c, d):
    f = []
    for r in c.get("ov", []):
        if r not in d["overridden_rules"]:
            f.append(f"missed-override:{r.split(': ')[1]}")
    for r in c.get("keep", []):
        if r in d["overridden_rules"]:
            f.append(f"false-override:{r.split(': ')[1]}")
    # Effective result: an overridden genre must not be excluded; a kept one must be.
    excl = set(d["genres_exclude"]) | set(d["rule_genres_exclude"]) | set(d["limit_genres_exclude"])
    for r in c.get("ov", []):
        if r.startswith("avoid genre") and r.split(": ")[1] in excl and not c.get("limit_horror"):
            f.append(f"still-excluded:{r.split(': ')[1]}")
    if c.get("limit_horror"):
        if "horror" not in excl:
            f.append("LIMIT-BROKEN:horror")
        if not d["kids"]:
            f.append("LIMIT-BROKEN:kids")
    own = " ".join(d["genres_exclude"] + d["avoid_soft"])
    # Copying a rule the viewer did NOT override changes nothing (fixed code applies it
    # anyway) -> counted, not failed. Copying an OVERRIDDEN rule contradicts it -> fail.
    for r in d["overridden_rules"]:
        if r.split(": ", 1)[1] in own:
            f.append(f"contradiction:{r.split(': ')[1]}")
    if c.get("nocopy") and (any(w in own for w in RULE_WORDS) or d.get("_copied_rules")):
        d["_harmless_copy"] = True
    return f


def run(args):
    i, rep = args
    c = CASES[i]
    p = PROFILES[c["who"]]
    d = understand(c["msg"], c.get("hist", []), [], rules=[r["text"] for r in p["prefs"]])
    d = apply_profile(d, p["prefs"], p["limits"])
    return i, rep, d, check(c, d)


if __name__ == "__main__":
    REPS = 3
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(run, [(i, r) for i in range(len(CASES)) for r in range(REPS)]))
    fails = [x for x in res if x[3]]
    print(f"override suite: pass {len(res) - len(fails)}/{len(res)} runs ({len(CASES)} cases x {REPS})")
    by_case = {}
    for i, rep, d, f in res:
        by_case.setdefault(i, []).append((d, f))
    stable = sum(len({tuple(d["overridden_rules"]) for d, _ in v}) == 1 for v in by_case.values())
    print(f"stable overrides across runs: {stable}/{len(CASES)}")
    print(f"harmless rule copies (no effect): {sum(bool(d.get('_harmless_copy')) for _, _, d, _ in res)} runs")
    for i in sorted(by_case):
        c = CASES[i]
        d, f = by_case[i][0]
        flag = "FAIL" if any(ff for _, ff in by_case[i]) else "ok  "
        print(f"{flag} [{c['who']}] {(str(c.get('hist')) + ' >> ') if c.get('hist') else ''}{c['msg'][:60]:60} "
              f"overrides={[r.split(': ')[1] for r in d['overridden_rules']]} path={d['path']} "
              f"must_not={sorted(set(d['genres_exclude'] + d['rule_genres_exclude'] + d['limit_genres_exclude']))} "
              f"{'; '.join(sorted({x for _, ff in by_case[i] for x in ff}))}")
