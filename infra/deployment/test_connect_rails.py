"""Connect Predictive Insights recommendations -> CONN# Home rows (infra/deployment/connect_rails.py)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/deployment"))

import connect_rails as cr  # noqa: E402
import personalize_rails as pr  # noqa: E402

NOW = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)
PID = "p-" + "a" * 32
CATALOG = {"a1": (["action"], 8.0), "a2": (["action", "drama"], 7.0), "a3": (["action"], 9.0),
           "d1": (["drama"], 9.5)}


class FakeConnect:
    """fetch() stand-in: records calls; the genre filter can be made to fail."""

    def __init__(self, top, genre_ok=True, rerank=None):
        self.top, self.genre_ok, self.rerank, self.calls = top, genre_ok, rerank, []

    def __call__(self, n, filters=None, candidates=None):
        self.calls.append({"n": n, "filters": filters, "candidates": candidates})
        names = [f["Name"] for f in filters or []]
        if "genre" in names:
            if not self.genre_ok:
                raise RuntimeError("InternalServerException")
            return [f"g-{filters[0]['Values']['GENRE']}"]
        if candidates:
            return list(reversed(candidates)) if self.rerank is None else self.rerank
        return self.top


def test_genre_candidates_ranks_by_rating_and_skips_excluded():
    assert cr.genre_candidates(CATALOG, "action") == ["a3", "a1", "a2"]
    assert cr.genre_candidates(CATALOG, "action", exclude={"a3"}, n=1) == ["a1"]


def test_catalog_genres_drops_episodes_and_games(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text('"title_id","title_type","genres","imdb_rating"\n'
                 'tt1,movie,Action; Drama,7.5\ntt2,tvEpisode,Action,8\ntt3,videoGame,Action,9\ntt4,movie,,6\n')
    assert cr.catalog_genres([p]) == {"tt1": (["action", "drama"], 7.5)}


def test_profile_rows_with_working_genre_filter():
    fake = FakeConnect(["h", "t1", "t2"])
    rows, notes = cr.profile_rows({"profile_id": PID, "genres": ["action"]}, fake, CATALOG, NOW)
    assert notes == {"action": "service filter"}
    assert rows[0] == {"profile_id": PID, "sk": "CONN#HERO#home", "title_id": "h",
                       "source": "connect", "generated_at": "2026-10-28T02:00:00Z"}
    assert [(r["sk"], r["display_title"]) for r in rows[1:]] == [
        ("CONN#ROWS#home#top_picks", "Top Picks for You"),
        ("CONN#ROWS#home#conn_genre_action", "Because You Like Action")]
    assert fake.calls[0]["filters"] == [{"Name": "exclude-watched"}]
    assert all(r["sk"].startswith("CONN#") and r["source"] == "connect" for r in rows)


def test_profile_rows_falls_back_to_candidate_rerank():
    fake = FakeConnect(["h", "a3"], genre_ok=False)
    rows, notes = cr.profile_rows({"profile_id": PID, "genres": ["action"], "watched": ["a2"]},
                                  fake, CATALOG, NOW)
    assert notes["action"].startswith("CandidateIds re-rank")
    rerank = fake.calls[-1]
    assert rerank["candidates"] == ["a1"]          # a3 is in Top Picks, a2 was watched
    assert rerank["filters"] is None               # Connect ignores filters with CandidateIds
    genre_row = rows[-1]
    assert genre_row["sk"] == "CONN#ROWS#home#conn_genre_action"
    assert [i["title_id"] for i in genre_row["items"]] == ["a1"]


def test_profile_rows_never_builds_because_you_watched():
    rows, _ = cr.profile_rows({"profile_id": PID}, FakeConnect(["h", "t"]), CATALOG, NOW)
    assert not any("because_you_watched" in r["sk"] for r in rows)


def test_mapped_entries_needs_connect_id_and_valid_profile():
    m = {"x": {"profile_id": PID, "connect_profile_id": "c" * 32}, "y": {"profile_id": PID}}
    assert [e["connect_profile_id"] for e in cr.mapped_entries(m)] == ["c" * 32]
    with pytest.raises(ValueError):
        cr.mapped_entries({"x": {"profile_id": "nope", "connect_profile_id": "c" * 32}})


def test_conn_key_guard_refuses_other_prefixes():
    rows, _ = cr.profile_rows({"profile_id": PID}, FakeConnect(["h"]), CATALOG, NOW)
    pr.check_keys(rows, ["CONN#ROWS#home#old"], cr.PREFIX)
    for bad in ("P13N#HERO#home", "HERO#home"):
        with pytest.raises(ValueError):
            pr.check_keys(rows, [bad], cr.PREFIX)
    assert pr.stale_keys(["CONN#ROWS#home#old", "P13N#HERO#home", "CONN#HERO#home"], rows, cr.PREFIX) == \
        ["CONN#ROWS#home#old"]
