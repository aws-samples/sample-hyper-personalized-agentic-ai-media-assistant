"""Removing a profile is soft: the member item gets `removed_at`, every other
row stays, and a restore brings the profile back with its data."""
import importlib
import json

import pytest

from test_household import (MemberStore, call, event, fn, household_with_seed, post,  # noqa: F401 (fixture)
                            FakeHouseholds)


class RemoveStore(MemberStore):
    """MemberStore plus set_removed, with the marker's member_seq lock."""

    def set_removed(self, account_id, seen_seq, profile_id, removed_at):
        marker = MemberStore.marker(self, account_id)
        if int(marker.get("member_seq") or 0) != seen_seq:
            raise self.fn.core.MemberError(409, "the household changed; try again")
        member = self.member(account_id, profile_id)
        if bool(removed_at) == bool(member.get("removed_at")):
            raise self.fn.core.MemberError(409, "the household changed; try again")
        marker["member_seq"] = seen_seq + 1
        if removed_at:
            member["removed_at"] = removed_at
        else:
            member.pop("removed_at")


def seeded(fn, monkeypatch):
    store = RemoveStore(fn)
    _, body = call(fn, store, monkeypatch, event("/household"))
    return store, {m["displayName"]: m["profileId"] for m in body["members"]}


def act(fn, store, monkeypatch, pid, action, sub="sub-1", groups=None, method="POST"):
    claims = {"sub": sub, **({"cognito:groups": groups} if groups else {})}
    e = {"resource": f"/household/members/{{profile_id}}/{action}", "httpMethod": method, "body": "{}",
         "pathParameters": {"profile_id": pid}, "requestContext": {"authorizer": {"claims": claims}}}
    return call(fn, store, monkeypatch, e)


def household(fn, store, monkeypatch, sub="sub-1"):
    return call(fn, store, monkeypatch, event("/household", sub=sub))[1]


def test_remove_hides_the_member_and_keeps_its_data(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    pid = ids["Rom-Com Lover"]
    rows_before = {k: [i for i in v if i.get("user_id") == pid or i.get("profile_id") == pid]
                   for k, v in store.tables.items() if k != "households"}
    status, body = act(fn, store, monkeypatch, pid, "remove")
    assert status == 200 and body["profileId"] == pid and body["removedAt"]
    h = household(fn, store, monkeypatch)
    assert [m["displayName"] for m in h["members"]] == ["Action Fan", "Sports Fan"]
    assert [m["displayName"] for m in h["removed"]] == ["Rom-Com Lover"]
    rows_after = {k: [i for i in v if i.get("user_id") == pid or i.get("profile_id") == pid]
                  for k, v in store.tables.items() if k != "households"}
    assert rows_after == rows_before and any(rows_after.values())


def test_removed_member_is_refused_by_home(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    act(fn, store, monkeypatch, ids["Action Fan"], "remove")
    assert call(fn, store, monkeypatch, event("/home", profile_id=ids["Action Fan"]))[0] == 404
    assert call(fn, store, monkeypatch, event("/home", profile_id=ids["Sports Fan"]))[0] == 200


def test_restore_brings_the_member_back_in_rank_order(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    pid = ids["Action Fan"]
    act(fn, store, monkeypatch, pid, "remove")
    status, body = act(fn, store, monkeypatch, pid, "restore")
    assert status == 200 and "removedAt" not in body
    h = household(fn, store, monkeypatch)
    assert [m["displayName"] for m in h["members"]] == ["Action Fan", "Rom-Com Lover", "Sports Fan"]
    assert h["removed"] == []
    assert call(fn, store, monkeypatch, event("/home", profile_id=pid))[0] == 200


def test_removed_member_frees_a_slot_and_restore_respects_the_limit(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    act(fn, store, monkeypatch, ids["Sports Fan"], "remove")
    for name in ["A", "B", "C"]:  # 2 active + 3 new = the limit of 5
        assert post(fn, store, monkeypatch, {"display_name": name})[0] == 201
    status, body = act(fn, store, monkeypatch, ids["Sports Fan"], "restore")
    assert status == 409 and "at most" in body["error"]


def test_new_member_rank_skips_removed_members(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    act(fn, store, monkeypatch, ids["Sports Fan"], "remove")  # rank 3
    _, body = post(fn, store, monkeypatch, {"display_name": "Sam"})
    assert body["rank"] == 4


def test_name_of_removed_member_can_be_reused_but_then_blocks_restore(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    act(fn, store, monkeypatch, ids["Sports Fan"], "remove")
    assert post(fn, store, monkeypatch, {"display_name": "sports fan"})[0] == 201
    status, body = act(fn, store, monkeypatch, ids["Sports Fan"], "restore")
    assert status == 409 and "name" in body["error"]


def test_last_profile_cannot_be_removed(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    act(fn, store, monkeypatch, ids["Action Fan"], "remove")
    act(fn, store, monkeypatch, ids["Rom-Com Lover"], "remove")
    status, body = act(fn, store, monkeypatch, ids["Sports Fan"], "remove")
    assert status == 409 and "at least one" in body["error"]


@pytest.mark.parametrize("action", ["remove", "restore"])
def test_twice_or_unknown_is_404(fn, monkeypatch, action):
    store, ids = seeded(fn, monkeypatch)
    pid = ids["Action Fan"]
    if action == "remove":
        act(fn, store, monkeypatch, pid, "remove")
    assert act(fn, store, monkeypatch, pid, action)[0] == 404
    assert act(fn, store, monkeypatch, "p-" + "0" * 32, action)[0] == 404
    assert act(fn, store, monkeypatch, "not-an-id", action)[0] == 404


def test_another_household_cannot_remove(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    household(fn, store, monkeypatch, sub="sub-2")
    assert act(fn, store, monkeypatch, ids["Action Fan"], "remove", sub="sub-2")[0] == 404
    assert len(household(fn, store, monkeypatch)["members"]) == 3


def test_booth_and_wrong_method_are_refused(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    assert act(fn, store, monkeypatch, ids["Action Fan"], "remove", groups="booth")[0] == 403
    assert act(fn, store, monkeypatch, ids["Action Fan"], "remove", method="GET")[0] == 404
    assert act(fn, store, monkeypatch, ids["Action Fan"], "purge")[0] == 404


def test_concurrent_change_loses_the_lock(fn, monkeypatch):
    store, ids = seeded(fn, monkeypatch)
    real = store.marker
    monkeypatch.setattr(store, "marker", lambda a: {**real(a), "member_seq": 99})
    assert act(fn, store, monkeypatch, ids["Action Fan"], "remove")[0] == 409


def test_seed_upgrade_never_readds_a_removed_seed_member(fn):
    core = fn.core
    seed = {"version": 2, "members": [{"member_key": "action-fan"}, {"member_key": "sports-fan"}]}
    members = [{"seed_member_key": "action-fan", "removed_at": "2026-10-21T00:00:00Z"}]
    to_add = core.seed_members_to_add(seed, {"seed_version": 1}, members)
    assert [m["member_key"] for m in to_add] == ["sports-fan"]


def test_shared_membership_check_refuses_removed_members(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("HOUSEHOLDS_TABLE", "households")
    mod = importlib.import_module("ibc_common")
    pid = "p-" + "b" * 32

    class Removed(FakeHouseholds):
        def get_item(self, Key):
            item = super().get_item(Key)
            if item:
                item["Item"]["removed_at"] = "2026-10-21T00:00:00Z"
            return item

    monkeypatch.setattr(mod._session, "resource", lambda *_: Removed({("sub-1", "MEMBER#" + pid)}))
    e = {"requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}
    assert mod.resolve_profile_pk(e, None, pid) is None


def test_member_routes_are_post_with_cognito():
    from test_household import application_template
    _, app = application_template()
    res = {k: v["Properties"] for k, v in app["Resources"].items() if v["Type"] == "AWS::ApiGateway::Resource"}
    [member] = [k for k, p in res.items() if p["PathPart"] == "{profile_id}"
                and res.get(p["ParentId"].get("Ref"), {}).get("PathPart") == "members"]
    actions = {k: p["PathPart"] for k, p in res.items() if p["ParentId"].get("Ref") == member}
    assert sorted(actions.values()) == ["remove", "restore"]
    methods = sorted((actions[v["Properties"]["ResourceId"]["Ref"]], v["Properties"]["HttpMethod"],
                      v["Properties"]["AuthorizationType"])
                     for v in app["Resources"].values() if v["Type"] == "AWS::ApiGateway::Method"
                     and v["Properties"].get("ResourceId", {}).get("Ref") in actions
                     and v["Properties"]["HttpMethod"] != "OPTIONS")
    assert methods == [("remove", "POST", "COGNITO_USER_POOLS"), ("restore", "POST", "COGNITO_USER_POOLS")]
