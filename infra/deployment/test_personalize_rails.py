"""Personalize batch output -> P13N# Home rows (infra/deployment/personalize_rails.py)."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/deployment"))

import personalize_rails as pr  # noqa: E402

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)
PID = "p-" + "a" * 32
OTHER = "p-" + "c" * 32


def rec(inp, items, error=None):
    return json.dumps({"input": inp, "output": {"recommendedItems": items} if items else None, "error": error})


def test_parse_batch_skips_errors_and_blanks():
    text = "\n".join([rec({"userId": "u1"}, ["a"]), "", rec({"userId": "u2"}, None, error="boom"),
                      rec({"userId": "u3"}, [])])
    assert [r["input"]["userId"] for r in pr.parse_batch(text)] == ["u1"]


def test_genre_slug_and_label():
    assert pr.genre_slug({"GENRE": '"sci-fi"'}) == "sci-fi"
    assert pr.genre_label("sci-fi") == "Sci-Fi" and pr.genre_label("sport") == "Sport"


def test_build_rows_dedupes_in_rail_order_and_ranks():
    top = ["h", "a", "b", "c"]
    rows = pr.build_rows(PID, top, [("action", ["a", "d", "e"]), ("sport", ["b"]), ("music", ["z"])],
                         ["c", "f"], "Burrowing Mole", NOW)
    assert rows[0] == {"profile_id": PID, "sk": "P13N#HERO#home", "title_id": "h",
                       "source": "personalize", "generated_at": "2026-10-27T03:00:00Z"}
    rails = {r["sk"]: (r["rank"], r["display_title"], [i["title_id"] for i in r["items"]]) for r in rows[1:]}
    assert rails == {
        "P13N#ROWS#home#top_picks": (1, "Top Picks for You", ["a", "b", "c"]),
        "P13N#ROWS#home#because_you_watched": (2, "Because You Watched Burrowing Mole", ["f"]),
        "P13N#ROWS#home#p13n_genre_action": (3, "Because You Like Action", ["d", "e"]),
    }   # sport rail emptied by de-duplication; music is past MAX_GENRE_RAILS
    assert all(r["sk"].startswith("P13N#") for r in rows)


def test_build_rows_caps_rail_lengths():
    rows = pr.build_rows(PID, [f"t{i}" for i in range(50)], [], None, None, NOW)
    assert len(rows[1]["items"]) == pr.TOP_N and len(rows) == 2


def test_plan_maps_users_and_ignores_synthetic_ones():
    top = pr.parse_batch("\n".join([rec({"userId": "persona-x"}, ["h", "a"]), rec({"userId": OTHER}, ["k"]),
                                    rec({"userId": "u-123"}, ["q"])]))
    genre = pr.parse_batch(rec({"userId": "persona-x", "filterValues": {"GENRE": '"action"'}}, ["g"]))
    sims = pr.parse_batch(rec({"itemId": "tt1"}, ["tt1", "s"]))
    mapping = {"persona-x": {"profile_id": PID, "anchor": "tt1"}}
    out = pr.plan(top, genre, sims, mapping, {"tt1": "Quantum Storm"}, NOW)
    assert set(out) == {PID, OTHER}               # u-123 is a training-only user
    byw = next(r for r in out[PID] if r["sk"].endswith("because_you_watched"))
    assert byw["display_title"] == "Because You Watched Quantum Storm"
    assert [i["title_id"] for i in byw["items"]] == ["s"]   # the anchor itself is dropped
    assert [r["sk"] for r in out[OTHER]] == ["P13N#HERO#home"]


def test_plan_rejects_a_bad_profile_id():
    with pytest.raises(ValueError):
        pr.plan(pr.parse_batch(rec({"userId": "persona-x"}, ["h"])), [], [],
                {"persona-x": {"profile_id": "nope"}}, {}, NOW)


def test_stale_keys_and_key_guard():
    rows = pr.build_rows(PID, ["h", "a"], [], None, None, NOW)
    existing = ["P13N#HERO#home", "P13N#ROWS#home#p13n_genre_old", "ROWS#home#top_picks", "HERO#home"]
    assert pr.stale_keys(existing, rows) == ["P13N#ROWS#home#p13n_genre_old"]
    pr.check_keys(rows, ["P13N#ROWS#home#x"])
    with pytest.raises(ValueError):
        pr.check_keys(rows, ["ROWS#home#top_picks"])
    with pytest.raises(ValueError):
        pr.check_keys([{**rows[0], "sk": "HERO#home"}], [])
