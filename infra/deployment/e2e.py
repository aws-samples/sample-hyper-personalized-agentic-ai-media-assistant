"""End-to-end check of a deployed, data-loaded environment.

    .venv/bin/python infra/deployment/e2e.py --config infra/environments/<name>.json

Run after smoke.py and after all loaders (load_media.py, load_catalog.py,
seed_video_mapping.py and the two match loaders). It signs in as a temporary
Cognito user (removed at the end) and exercises what a viewer does:

- GET /household provisions the sample household; GET /home returns rows of
  catalog titles with presigned posters.
- The movie runtime recommends catalog titles for a typed request, and a
  micro-drama request returns playable micro-dramas.
- GET /get_video_url returns a playable URL for a micro-drama.
- POST /clips/session returns a feed with micro-drama videos.
- GET /state is spoiler-safe: 1-1 before the clip's goal, 2-1 after it.
- The sports runtime answers about the score at the playhead.
"""
import argparse
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from config import DEFAULT_CONFIG, load_config, stack_name
from deploy import aws_environment

ROOT = Path(__file__).resolve().parents[2]
GOAL_VIDEO_S = 25.84  # backend/live-sports-agent: the 2-1 goal in the football clip
TOS_ID = "tt2285752"  # Tears of Steel, set up by notebooks/tears-of-steel/
TOS_SCENE_MS = 60_000   # one minute in: "40 years later", the holograms
TOS_ACTOR_MS = 100_000  # Old Thom (Derek de Lint), recognized by Rekognition in the notebook


def catalog_ids():
    ids = set()
    for path in (ROOT / "data/catalog").glob("*.json"):
        ids.update(t["title_id"] for t in json.loads(path.read_text()))
    return ids


def http(url, token=None, data=None, headers=None, timeout=120):
    hdrs = {**({"Authorization": token} if token else {}), **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def sse_events(raw):
    """Decode an SSE body into JSON events (non-JSON data lines are skipped)."""
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return out


def remove_household(dynamodb, prefix, account_id):
    """Delete a household and everything keyed by its members' profile ids
    (profile, recommendation-cache and user-interactions items). Returns the
    number of items deleted. Used for the temporary user, so its personas do not
    stay in the insights console after the check."""
    from boto3.dynamodb.conditions import Key

    def items(table, key, value):
        found, kw = [], {"KeyConditionExpression": Key(key).eq(value)}
        while True:
            page = table.query(**kw)
            found += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return found
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    households = dynamodb.Table(f"{prefix}-households")
    rows = items(households, "account_id", account_id)
    profile_ids = [r["sk"].split("#", 1)[1] for r in rows if r["sk"].startswith("MEMBER#")]
    deleted = 0
    for name, pk, sk in [("profiles", "user_id", "data_type"), ("recommendation-cache", "profile_id", "sk"),
                         ("user-interactions", "profile_id", "sk")]:
        table = dynamodb.Table(f"{prefix}-{name}")
        with table.batch_writer() as batch:
            for pid in profile_ids:
                for item in items(table, pk, pid):
                    batch.delete_item(Key={pk: item[pk], sk: item[sk]})
                    deleted += 1
    with households.batch_writer() as batch:
        for r in rows:
            batch.delete_item(Key={"account_id": r["account_id"], "sk": r["sk"]})
            deleted += 1
    return deleted


def check(cond, label, detail=""):
    if not cond:
        raise AssertionError(f"FAIL: {label} {detail}"[:2000])
    print("PASS:", label)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    cfg = load_config(args.config)
    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    assert session.client("sts").get_caller_identity()["Account"] == cfg["account"]
    outputs = {}
    for suffix in ("Application", "Frontend"):
        stack = session.client("cloudformation").describe_stacks(StackName=stack_name(cfg, suffix))["Stacks"][0]
        outputs.update({o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]})
    api = outputs["ApiUrl"].rstrip("/")
    known = catalog_ids()

    def invoke(arn, payload, token):
        url = (session.client("bedrock-agentcore").meta.endpoint_url + "/runtimes/"
               + urllib.parse.quote(arn, safe="") + "/invocations?qualifier=DEFAULT")
        return http(url, "Bearer " + token, payload,
                    {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4())}, timeout=300)

    cognito = session.client("cognito-idp")
    pool, client = outputs["UserPoolId"], outputs["UserPoolClientId"]
    username = "e2e-check-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(24) + "aA1!"
    cognito.admin_create_user(UserPoolId=pool, Username=username, MessageAction="SUPPRESS")
    try:
        cognito.admin_set_user_password(UserPoolId=pool, Username=username, Password=password, Permanent=True)
        auth = cognito.initiate_auth(ClientId=client, AuthFlow="USER_PASSWORD_AUTH",
                                     AuthParameters={"USERNAME": username, "PASSWORD": password})["AuthenticationResult"]
        id_token, access = auth["IdToken"], auth["AccessToken"]

        # Household + Home
        status, body = http(api + "/household", id_token)
        check(status == 200, "GET /household", body[:300])
        members = json.loads(body).get("members", [])
        check(len(members) >= 2, f"household has {len(members)} members", body[:300])
        print("  members:", ", ".join(f'{m.get("memberKey") or m.get("displayName")}={m["profileId"]}' for m in members))
        member = members[0]
        status, body = http(api + "/home?" + urllib.parse.urlencode({"profile_id": member["profileId"]}), id_token)
        check(status == 200, "GET /home", body[:300])
        home = json.loads(body)
        rail_ids = [t.get("id") or t.get("title_id") for r in home.get("rails", []) for t in r.get("items", r.get("movies", []))]
        check(rail_ids and set(rail_ids) <= known, f"home rails list {len(rail_ids)} catalog titles",
              str(set(rail_ids) - known)[:300])
        posters = [t.get("posterUrl") or t.get("image") for r in home.get("rails", []) for t in r.get("items", r.get("movies", []))]
        signed = [p for p in posters if p and p.startswith("https://")]
        check(signed, f"home posters presigned ({len(signed)}/{len(posters)})", str(posters[:2]))
        status, _ = http(signed[0], timeout=60)
        check(status == 200, "poster downloads")

        # Movie recommendations (typed search)
        movie_arn = outputs["MovieRuntimeArn"]
        for query, want_md in (("Recommend a funny animated movie for family night", False),
                               ("Show me a micro drama", True)):
            status, body = invoke(movie_arn, {"session_id": str(uuid.uuid4()), "profile": member["profileId"],
                                              "user_query": query, "request_type": "recommendation"}, access)
            check(status == 200, f"movie runtime: {query!r}", body[:300])
            final = [e for e in sse_events(body) if e.get("type") == "response"]
            check(final, "movie runtime returned a response event", body[-500:])
            recs = final[-1].get("movie-recommendation", [])
            ids = [r.get("title_id") for r in recs]
            check(ids and set(ids) <= known, f"recommended {len(ids)} catalog titles: "
                  + ", ".join(r.get("originalTitle", "?") for r in recs), str(final[-1])[:500])
            if want_md:
                check(any(i.startswith("md") for i in ids), "micro-drama request returns micro-dramas", str(ids))
                check(final[-1].get("content_urls"), "micro-dramas come with playable URLs", str(final[-1])[:500])

        # Playback URL
        status, body = http(api + "/get_video_url?" + urllib.parse.urlencode({"title_id": "md0000001", "user_id": username}), id_token)
        check(status == 200, "GET /get_video_url micro-drama", body[:300])
        status, head = http(json.loads(body)["url"], headers={"Range": "bytes=0-11"}, timeout=60)
        check(status in (200, 206) and head[4:8] == b"ftyp", "micro-drama video streams")

        # Clips feed
        status, body = http(api + "/clips/session", id_token, {"profile_id": member["profileId"], "session_id": str(uuid.uuid4())})
        check(status == 200, "POST /clips/session", body[:300])
        cards = json.loads(body).get("cards", [])
        check(any(c.get("kind") == "microdrama" and c.get("videoUrl") for c in cards),
              f"clips feed has micro-drama videos ({len(cards)} cards)", body[:300])

        # Tears of Steel (optional: notebooks/tears-of-steel/1-video-scenes.ipynb)
        status, body = http(api + "/get_video_url?" + urllib.parse.urlencode({"title_id": TOS_ID, "user_id": username}), id_token)
        if status == 404:
            print("SKIP: Tears of Steel is not set up (run notebooks/tears-of-steel/1-video-scenes.ipynb)")
        else:
            check(status == 200, "GET /get_video_url Tears of Steel", body[:300])
            status, head = http(json.loads(body)["url"], headers={"Range": "bytes=0-11"}, timeout=60)
            check(status in (200, 206) and head[4:8] == b"ftyp", "Tears of Steel streams")
            for question, millis, label in (("What's happening in this scene?", TOS_SCENE_MS, "scene"),
                                            ("Who is the actor in this scene?", TOS_ACTOR_MS, "actors")):
                status, body = invoke(movie_arn, {"session_id": str(uuid.uuid4()), "profile": member["profileId"],
                                                  "user_query": question, "request_type": "analysis",
                                                  "timestamp": millis, "title_id": TOS_ID,
                                                  "video_name": "tears-of-steel.mp4"}, access)
                check(status == 200, f"scene agent: {question!r}", body[:300])
                events = sse_events(body)
                text = " ".join(str(e.get("response", "")) for e in events if e.get("type") == "response").strip()
                print(f"  {label}: {text[:300]}")
                check(text and "aren't available" not in text and "couldn't find scene" not in text,
                      f"scene agent answers from the scene index ({label})", str(events)[-500:])
                if label == "actors":
                    check("de Lint" in text, "scene agent names the recognized actor", text[:300])

        # Tears of Steel title-page highlight, chosen per profile taste
        picks = {}
        for m in members:
            key = m.get("memberKey") or m.get("displayName")
            status, body = http(api + "/get_video_url?" + urllib.parse.urlencode(
                {"title_id": TOS_ID, "highlight": "1", "profile_id": m["profileId"]}), id_token)
            if status == 404:
                break
            check(status == 200, f"highlight for {key}", body[:300])
            picks[key] = json.loads(body)["clip"]
        if not picks:
            print("SKIP: Tears of Steel highlights are not set up")
        else:
            print("  highlight picks:", picks)
            if "action-fan" in picks and "rom-com-lover" in picks:
                check(picks["action-fan"].endswith("_action_fan.mp4")
                      and picks["rom-com-lover"].endswith("_rom_com_lover.mp4"),
                      "action fan gets the action cut, rom-com lover the romantic cut")

        # Live match state: spoiler-safe score around the goal
        scores = {}
        for t in (GOAL_VIDEO_S - 5, GOAL_VIDEO_S + 2):
            status, body = http(api + f"/state?video_s={t:.2f}", id_token)
            check(status == 200, f"GET /state video_s={t:.2f}", body[:300])
            scores[t] = sorted(json.loads(body)["score"].values())
        check(scores[GOAL_VIDEO_S - 5] == [1, 1] and scores[GOAL_VIDEO_S + 2] == [1, 2],
              f"score is 1-1 before the goal and 2-1 after it {scores}")

        # Sports agent
        sports_arn = outputs["SportsRuntimeArn"]
        answers = {}
        for t in (GOAL_VIDEO_S - 5, GOAL_VIDEO_S + 2):
            status, body = invoke(sports_arn, {"question": "What's the score right now? Answer with the score only.",
                                               "video_s": t, "history": []}, access)
            check(status == 200, f"sports runtime video_s={t:.2f}", body[:300])
            events = sse_events(body)
            check(not any("error" in e for e in events), "sports runtime has match data", str(events)[:300])
            answers[t] = "".join(e.get("delta", "") for e in events)
            print(f"  answer at {t:.2f}s: {answers[t].strip()[:160]}")
        before, after = answers[GOAL_VIDEO_S - 5], answers[GOAL_VIDEO_S + 2]
        check("1" in before and "2" not in before, "agent reports 1-1 before the goal (no spoiler)")
        check("2" in after, "agent reports 2-1 after the goal")
    finally:
        attrs = cognito.admin_get_user(UserPoolId=pool, Username=username)["UserAttributes"]
        sub = next(a["Value"] for a in attrs if a["Name"] == "sub")
        removed = remove_household(session.resource("dynamodb"), cfg["prefix"], sub)
        cognito.admin_delete_user(UserPoolId=pool, Username=username)
        print(f"Temporary user removed, with its household ({removed} items)")


if __name__ == "__main__":
    main()
