"""Intent eval: question about shown titles ('answer') vs a request for titles ('recommend').

Live Bedrock calls. Run from evals/:  python run_intent.py .. [reps]
Cases use previously shown titles (prev) the way the agent passes them.
"""
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])
from understand import understand  # noqa: E402

REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
SHOWN = ["The Grand Budapest Hotel", "Paddington 2", "The Lego Batman Movie", "Crazy, Stupid, Love.", "Frances Ha"]
THRILL = ["Knives Out", "The Nice Guys", "Hot Fuzz", "Kingsman: The Secret Service", "Kiss Kiss Bang Bang"]
A, R = "answer", "recommend"
CASES = [
    (["something funny"], SHOWN, "who directed the first one?", A),
    (["something funny"], SHOWN, "what's Paddington 2 about?", A),
    (["something funny"], SHOWN, "is the lego one ok for my 6 year old?", A),
    (["something funny"], SHOWN, "which of these is the funniest?", A),
    (["something funny"], SHOWN, "how long is Frances Ha", A),
    (["something funny"], SHOWN, "why did you pick Crazy Stupid Love for me", A),
    (["something funny"], SHOWN, "who's in the grand budapest hotel", A),
    (["a thriller"], THRILL, "is Knives Out a murder mystery?", A),
    (["a thriller"], THRILL, "which one should I start with tonight", A),
    (["a thriller"], THRILL, "are the nice guys and kiss kiss bang bang by the same director?", A),
    (["a thriller"], THRILL, "thanks!", A),
    (["a thriller"], THRILL, "is hot fuzz scary", A),
    (["something funny"], SHOWN, "more like the second one", R),
    (["something funny"], SHOWN, "something else, none of these", R),
    (["something funny"], SHOWN, "show me more like Paddington", R),
    (["something funny"], SHOWN, "any of these but older?", R),
    (["something funny"], SHOWN, "do you have Amelie?", R),
    (["something funny"], SHOWN, "a romcom instead", R),
    (["a thriller"], THRILL, "more like Knives Out but darker", R),
    (["a thriller"], THRILL, "what else is there from the director of Hot Fuzz?", R),
    (["a thriller"], THRILL, "something scarier", R),
    ([], [], "what should I watch tonight?", R),
    ([], [], "who directed Inception?", R),  # not shown yet: look it up (shows the card)
    (["a thriller"], THRILL, "tell me about the third one", A),
]


def run(args):
    i, _ = args
    hist, prev, msg, want = CASES[i]
    d = understand(msg, hist, prev, rules=["avoid theme: gore"])
    return i, d["intent"], d["_s"]


with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(run, [(i, r) for i in range(len(CASES)) for r in range(REPS)]))
by = {}
for i, intent, s in res:
    by.setdefault(i, []).append(intent)
ok = sum(intent == CASES[i][3] for i, intent, _ in res)
print(f"intent suite: pass {ok}/{len(res)} runs ({len(CASES)} cases x {REPS}); "
      f"stable {sum(len(set(v)) == 1 for v in by.values())}/{len(CASES)}")
for i in sorted(by):
    flag = "ok  " if all(x == CASES[i][3] for x in by[i]) else "FAIL"
    print(f"{flag} want={CASES[i][3]:9} got={by[i]} {CASES[i][2]!r}")
