"""docs/how-personalization-works.md stays true to the code.

The explainer quotes a few key numbers, one worked example and file paths, and
embeds a generated figure. Each test below fails when the code moves and the
doc does not, naming what to update:

- the key-numbers table matches every constant at its stated location;
- constants that are copied between components agree everywhere;
- the worked example equals what the production functions compute;
- the figure is what scripts/build_personalization_figures.py draws now;
- every repo path the doc names exists.
"""

import ast
import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/how-personalization-works.md"
TEXT = DOC.read_text()
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))


def _value(node):
    """A module-level constant's value, without importing the module: literals,
    arithmetic on literals, and int/float(os.environ.get(NAME, "default"))."""
    try:
        return ast.literal_eval(node)
    except ValueError:
        pass
    if isinstance(node, ast.BinOp):
        ops = {ast.Mult: lambda a, b: a * b, ast.Add: lambda a, b: a + b,
               ast.Sub: lambda a, b: a - b, ast.Div: lambda a, b: a / b}
        return ops[type(node.op)](_value(node.left), _value(node.right))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("int", "float")
            and isinstance(node.args[0], ast.Call) and len(node.args[0].args) == 2):
        return {"int": int, "float": float}[node.func.id](ast.literal_eval(node.args[0].args[1]))
    raise ValueError(f"cannot evaluate {ast.dump(node)}")


def const(path, name):
    tree = ast.parse((ROOT / path).read_text())
    for stmt in tree.body:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else []
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return _value(stmt.value)
    raise AssertionError(f"{name} not found at module level in {path}")


def table_rows():
    section = TEXT.split("## 6. Key numbers", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \| `(.+?)` \| .*? \| `([^`]+)` \|$", section, re.M)
    assert len(rows) >= 6, "key-numbers table not found or reformatted"
    return rows


@pytest.mark.parametrize("name,value,path", table_rows())
def test_constants_table_matches_code(name, value, path):
    assert const(path, name) == ast.literal_eval(value), (
        f"{name} in {path} changed: update the key-numbers table in {DOC.name}")


COPIES = [
    ("TASTE_JOIN_COSINE", ["infra/lambdas/profile_events/l2.py", "infra/lambdas/household/household_core.py",
                           "backend/movie-ai-agent-v2/taste.py", "insights/api/profile.py"]),
    ("TASTE_MAX_CENTROIDS", ["infra/lambdas/profile_events/l2.py", "infra/lambdas/household/household_core.py",
                             "backend/movie-ai-agent-v2/taste.py", "insights/api/profile.py"]),
] + [(name, ["infra/lambdas/profile_events/l2.py", "infra/lambdas/household/household_core.py",
             "backend/movie-ai-agent-v2/taste.py", "insights/api/profile.py"])
     for name in ("TASTE_EVIDENCE_K", "TASTE_TITLE_E", "CENTROID_RATE")]


@pytest.mark.parametrize("name,paths", COPIES)
def test_copied_constants_agree(name, paths):
    values = {p: const(p, name) for p in paths}
    assert len(set(values.values())) == 1, f"{name} differs between copies: {values}"


def test_half_lives_and_evidence_gates_agree():
    genre = const("infra/lambdas/profile_events/l2.py", "HALF_LIFE_DAYS")
    assert const("backend/movie-ai-agent-v2/user_profile.py", "_HALF_LIFE_DAYS") == genre
    assert const("insights/api/profile.py", "HALF_LIFE_DAYS") == genre
    assert const("infra/lambdas/household/cold_start.py", "GENRE_HALF_LIFE_DAYS") == genre["genre"]
    gate = const("infra/lambdas/profile_events/l2.py", "LOVED_N")
    assert const("backend/movie-ai-agent-v2/user_profile.py", "TASTE_MIN_N") == gate
    assert const("insights/api/profile.py", "TASTE_MIN_N") == gate
    assert const("infra/lambdas/household/cold_start.py", "MIN_EVIDENCE") == gate


def _in_doc(number):
    assert number in TEXT, f"worked example changed: {number!r} is no longer in {DOC.name}"


def test_entity_worked_example():
    l2 = importlib.import_module("l2")
    day = 86400
    ents = {}
    l2.seed_loved(ents, [{"id": "Sci-Fi", "label": "Sci-Fi"}], 0)
    assert (ents["genre:sci-fi"]["s"], ents["genre:sci-fi"]["n"]) == (0.5, 3)
    _in_doc(f"| 0 | picks Sci-Fi at sign-up | {ents['genre:sci-fi']['s']:.2f} |")
    e = l2.view_engagement({"type": "movie", "watched_s": 6000, "completion": 0.95})
    l2.update_entities(ents, {"genre:sci-fi": "Sci-Fi"}, e, 10 * day)
    stored = ents["genre:sci-fi"]["s"]
    _in_doc(f"| 10 | finishes a sci-fi film | {stored:.2f} |")
    _in_doc(f"faded to {stored * 2 ** (-60 / l2.HALF_LIFE_DAYS['genre']):.2f}")
    assert l2.ALPHA * l2.PARENT_FACTOR == l2.ALPHA / 2, "the doc says a clip counts half"


def _agent_time_bucket():
    """The agent's copy of time_bucket, loaded from source without importing the
    agent (which needs its runtime dependencies)."""
    path = ROOT / "backend/movie-ai-agent-v2/user_profile.py"
    tree = ast.parse(path.read_text())
    keep = [s for s in tree.body if (isinstance(s, ast.FunctionDef) and s.name == "time_bucket")
            or (isinstance(s, ast.Assign) and any(getattr(t, "id", "") in ("DAY_START_H", "EVENING_H")
                                                  for t in s.targets))]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns)  # noqa: S102 -- repo source
    return ns["time_bucket"]


def test_time_slot_rule_matches_between_processor_and_agent():
    l1 = importlib.import_module("l1")
    agent = _agent_time_bucket()
    start = 1792368000                     # a Monday, 00:00 UTC
    for offset in (-480, -300, 0, 60, 330, 540):
        for hour in range(0, 24 * 7, 1):
            ts = start + hour * 3600 + 1800
            assert l1.time_bucket(ts, offset) == agent(ts, offset), (ts, offset)
    assert l1.time_bucket(start, None) is None and agent(start, None) is None


def test_time_of_day_rules():
    assert const("backend/movie-ai-agent-v2/user_profile.py", "BUCKET_MIN_VIEWS") == 3, "update the Time of day section"
    assert (const("infra/lambdas/profile_events/l1.py", "DAY_START_H"),
            const("infra/lambdas/profile_events/l1.py", "EVENING_H")) == (5, 17), "update the Time of day section"
    # Only watched views update a slot: update_context runs for kind == "view", not for a mark.
    src = (ROOT / "infra/lambdas/profile_events/processor.py").read_text()
    assert re.search(r'if kind == "view" and view\.get\("reason"\) != "marked":\s*\n(\s*#.*\n)?\s*l2\.update_context\(',
                     src), "update the Time of day section"


def test_storage_example():
    """The AFFINITIES JSON in "What it looks like in storage" equals what the
    processor's functions build for the week described above it."""
    import json
    l1, l2 = importlib.import_module("l1"), importlib.import_module("l2")
    mon = 1792461600                      # Monday 21:00 US Central (UTC-5)
    both, drama = {"genre:sci-fi": "Sci-Fi", "genre:drama": "Drama"}, {"genre:drama": "Drama"}
    week = [(mon, "view", both), (mon + 86400, "view", drama), (mon + 2 * 86400, "view", both),
            (mon + 3 * 86400 - 9 * 3600, "list_add", drama), (mon + 5 * 86400 - 3600, "view", drama)]
    entities, contexts = {}, {}
    for ts, kind, labels in week:
        if kind == "view":
            view = {"type": "movie", "watched_s": 6600, "completion": 0.95, "ts": ts,
                    "bucket": l1.time_bucket(ts, -300)}
            e = l2.view_engagement(view)
            l2.update_entities(entities, labels, e, ts)
            l2.update_context(contexts, view, labels, e)
        else:
            l2.update_entities(entities, labels, l2.LIST_ADD_E, ts)
    block = TEXT.split("### What it looks like in storage", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    doc = json.loads(block)
    assert doc["entities"] == entities, "update the storage example's top-level entities"
    assert doc["contexts"] == contexts, "update the storage example's contexts"


def test_figures_are_current():
    spec = importlib.util.spec_from_file_location("build_figures", ROOT / "scripts/build_personalization_figures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--check"]) == 0, "figure is stale: run python scripts/build_personalization_figures.py"
    for name in mod.FIGURES:
        assert f"](figures/{name})" in TEXT, f"{name} is generated but not embedded in the doc"


def test_named_paths_exist():
    paths = set(re.findall(r"`((?:infra|backend|insights|scripts|frontend)/[\w./-]+\.\w+)`", TEXT))
    assert paths, "no repo paths found in the doc"
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert not missing, f"the doc names files that do not exist: {missing}"
