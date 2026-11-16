"""40 test cases. None of these phrases appear in the candidate prompt.

Fields: msg, hist (earlier requests), who (A=Action Fan, R=Rom-Com Lover),
paths (allowed routes), avoid (concepts that must survive), hard (concepts that
must be hard genre excludes), soft (concepts that must NOT be hard-excluded:
'too X' style), notavoid (concepts that must NOT be avoided), want (concepts
that must be in the positive side), leak (check persona genres weren't added).
"""
T, F, C = "taste", "fanout", "catch_all"
ANY = (T, F, C)

CASES = [
    # --- open request + avoid ---
    dict(msg="what should I watch tonight? I don't want anything too romantic", who="R", paths=(T,), avoid=["romance"], soft=["romance"], leak=True),
    dict(msg="surprise me, but no horror please", who="A", paths=(T,), avoid=["horror"], hard=["horror"], leak=True),
    dict(msg="anything, as long as it's not a musical", who="R", paths=(T,), avoid=["musical"], hard=["musical"], leak=True),
    dict(msg="I don't want to cry tonight", who="R", paths=(T,), avoid=["sad"], leak=True),
    dict(msg="nothing too long, I have to be up early", who="A", paths=(T,), avoid=["long"], leak=True),
    dict(msg="not in the mood for action", who="A", paths=(T,), avoid=["action"], notwant=["action"], leak=True),
    dict(msg="I hate horror and I'm not big on war films either, what's good?", who="A", paths=(T,), avoid=["horror", "war"], hard=["horror"], leak=True),
    dict(msg="anything except animation", who="R", paths=(T,), avoid=["animation"], hard=["animation"], leak=True),
    dict(msg="neither romance nor comedy tonight", who="R", paths=(T,), avoid=["romance", "comedy"], hard=["romance", "comedy"], notwant=["romance", "comedy"], leak=True),
    dict(msg="anything but sports", who="A", paths=(T,), avoid=["sport"], hard=["sport"], leak=True),
    dict(msg="Not a big fan of subtitles", who="A", paths=(T,), avoid=["subtitles"], leak=True),
    dict(msg="I'm tired of superhero movies, give me something else", who="A", paths=ANY, avoid=["superhero"], soft=["action"], leak=True),
    dict(msg="I've seen all the Nolan films, recommend something else", who="A", paths=ANY, avoid=["nolan"], leak=True),
    # --- specific + avoid ---
    dict(msg="something light tonight, nothing too violent, maybe from the last few years", who="R", paths=(C,), avoid=["violence"], soft=["action", "thriller", "crime", "war"], leak=True),
    dict(msg="my wife and I want something we both like, not a rom-com and not gory", who="R", paths=ANY, avoid=["romance", "gore"], notwant=["romance"], leak=True),
    dict(msg="a thriller, but not too dark or depressing", who="A", paths=(F, C), avoid=["dark"], want=["thriller"], soft=["thriller", "drama"]),
    dict(msg="sci-fi without the gore", who="A", paths=(F, C), avoid=["gore"], want=["scifi"]),
    dict(msg="comedies but nothing raunchy, the kids are watching", who="R", paths=(C,), avoid=["raunchy"], want=["comedy"]),
    dict(msg="something like Inception but less confusing", who="A", paths=ANY, avoid=["confusing"], want=["inception"]),
    dict(msg="romance, but not cheesy", who="R", paths=(F, C), avoid=["cheesy"], want=["romance"], soft=["romance"]),
    dict(msg="a feel-good movie that isn't a romcom", who="R", paths=ANY, avoid=["romance"], notwant=["romance"]),
    dict(msg="a movie for date night, nothing too steamy", who="R", paths=(F, C), avoid=["steamy"]),
    dict(msg="I'm not sure, maybe something funny? not slapstick though", who="R", paths=(F, C), avoid=["slapstick"], want=["comedy"]),
    dict(msg="Tom Hanks but not the war ones", who="A", paths=(C,), avoid=["war"], want=["hanks"]),
    dict(msg="90s movies, no dramas", who="A", paths=(C,), avoid=["drama"], hard=["drama"]),
    dict(msg="something my 8 year old and I can watch, no scary stuff", who="R", paths=(C,), avoid=["scary"]),
    dict(msg="I want to laugh, not think", who="A", paths=(F, C), avoid=["cerebral"], want=["comedy"]),
    dict(msg="recent stuff only, and skip anything with Adam Sandler", who="R", paths=(C,), avoid=["sandler"]),
    dict(msg="give me a crime movie that isn't too bloody", who="A", paths=(F, C), avoid=["gore"], want=["crime"], soft=["crime"]),
    dict(msg="a sad movie", who="R", paths=(F, C), want=["sad"]),
    # --- plain open (regression) ---
    dict(msg="surprise me", who="A", paths=(T,), leak=True),
    dict(msg="what should I watch tonight?", who="R", paths=(T,), leak=True),
    dict(msg="dunno", who="R", paths=(T,), leak=True),
    dict(msg="don't show me anything I've already watched", who="A", paths=(T,), leak=True),
    # --- multi-turn ---
    dict(hist=["funny micro dramas"], msg="but nothing romantic", who="R", paths=(C,), avoid=["romance"], want=["micro"]),
    dict(hist=["sci-fi thrillers"], msg="something less intense", who="A", paths=(F, C), avoid=["intense"], want=["scifi"]),
    dict(hist=["surprise me, no horror please"], msg="more like that", who="A", paths=ANY, avoid=["horror"]),
    dict(hist=["comedies without romance"], msg="actually romance is fine, just not sad", who="R", paths=(F, C), avoid=["sad"], notavoid=["romance"]),
    dict(hist=["action movies"], msg="anything else, I've had enough action", who="A", paths=(T, F), avoid=["action"], notwant=["action"]),
    dict(hist=["a thriller"], msg="you pick", who="A", paths=(F, C), want=["thriller"]),
]

STEMS = {
    "romance": ["romanc", "romant", "rom-com", "romcom", "love stor"],
    "horror": ["horror", "scary"], "scary": ["scar", "horror", "frighten"],
    "musical": ["musical"], "sad": ["sad", "cry", "tear", "depress", "melanchol", "tragic", "heavy", "emotional"],
    "long": ["long", "runtime", "length", "short"], "action": ["action"], "war": ["war"],
    "animation": ["animat", "cartoon"], "comedy": ["comed", "funny", "laugh", "humor", "humour"],
    "sport": ["sport"], "subtitles": ["subtit", "foreign", "non-english", "english"],
    "superhero": ["superhero", "super hero", "comic"], "nolan": ["nolan"],
    "violence": ["violen"], "gore": ["gor", "blood"], "dark": ["dark", "depress", "bleak", "grim"],
    "raunchy": ["raunch", "crude", "explicit", "adult", "vulgar", "sexual"],
    "confusing": ["confus", "complex", "convolut", "complicated"], "cheesy": ["chees", "cliche", "cliché", "sappy", "corny"],
    "steamy": ["steam", "sex", "explicit", "racy", "erotic"], "slapstick": ["slapstick"],
    "drama": ["drama"], "cerebral": ["think", "cerebral", "complex", "heavy", "intellectual"],
    "sandler": ["sandler"], "intense": ["intens", "dark", "heavy", "tense"],
    "thriller": ["thriller"], "scifi": ["sci-fi", "science fiction", "scifi"], "crime": ["crime"],
    "inception": ["inception"], "hanks": ["hanks"], "micro": ["micro"],
}
GENRE_OF = {"romance": "romance", "horror": "horror", "musical": "musical", "action": "action",
            "war": "war", "animation": "animation", "comedy": "comedy", "sport": "sport",
            "drama": "drama", "thriller": "thriller", "crime": "crime", "scifi": "sci-fi"}
PERSONA_GENRES = {"A": ["action", "sci-fi", "drama"], "R": ["comedy", "romance", "family"]}
PREFS = {
    "A": "Viewer profile (seeded preferences):\n- Favorite genres: action, scifi, drama\n- Favorite directors: Christopher Nolan, Denis Villeneuve, Bong Joon-ho\n- Preferred era: 2010s-present\n- Avoid themes: excessive gore, animal cruelty",
    "R": "Viewer profile (seeded preferences):\n- Favorite genres: comedy, romance, family\n- Favorite directors: Nora Ephron, Rob Reiner, Billy Wilder\n- Preferred era: 2000s-present\n- Avoid themes: excessive gore",
}

# Held-out set: written after round 1, never used to tune the prompt.
HELDOUT = [
    dict(msg="pick something for me but please, no more zombie stuff", who="A", paths=(T,), avoid=["zombie"], leak=True),
    dict(msg="can you find me a film tonight that isn't a downer", who="R", paths=(T, F), avoid=["sad"], leak=True),
    dict(msg="whatever you think, just keep it under two hours", who="A", paths=(T,), avoid=["long"], leak=True),
    dict(msg="I'm in the mood for something, just not a western", who="A", paths=(T,), avoid=["western"], hard=["western"], leak=True),
    dict(msg="no kids movies tonight, it's adults only", who="R", paths=ANY, avoid=["family"], leak=True),
    dict(msg="something scary but not slasher-level gross", who="A", paths=(F, C), avoid=["gore"], want=["horror"], soft=["horror"]),
    dict(msg="an adventure the whole family can enjoy, nothing with too much fighting", who="R", paths=(C,), avoid=["violence"], want=["adventure"], soft=["action"]),
    dict(msg="funny but not dumb", who="A", paths=(F, C), avoid=["dumb"], want=["comedy"]),
    dict(msg="a love story that doesn't end badly", who="R", paths=(F, C), avoid=["sad"], want=["romance"], soft=["romance"]),
    dict(msg="I've had enough of Marvel, show me something smart", who="A", paths=(F, C), avoid=["superhero"]),
    dict(msg="spy movies from the 2000s, but skip the Bond ones", who="A", paths=(C,), avoid=["bond"], want=["spy"]),
    dict(msg="not documentaries, not dramas, something fun", who="R", paths=(F, C), avoid=["documentary", "drama"], hard=["documentary", "drama"]),
    dict(hist=["cozy mysteries"], msg="less murder-y please", who="R", paths=(F, C), avoid=["violence"], want=["mystery"]),
    dict(hist=["horror films"], msg="ok forget horror, anything good?", who="A", paths=(T, F), notwant=["horror"]),
    dict(hist=["romantic comedies, nothing too sappy"], msg="show me a few more", who="R", paths=(F, C), avoid=["cheesy"], want=["romance"]),
]
STEMS.update({"adventure": ["adventur"], "mystery": ["myster"], "zombie": ["zombie", "undead"], "western": ["western"], "family": ["kid", "child", "family", "animat"],
              "dumb": ["dumb", "stupid", "silly", "mindless", "low-brow", "lowbrow", "crude"],
              "bond": ["bond", "007"], "spy": ["spy", "espionage"], "documentary": ["documentar"],
              "violence": ["violen", "fight", "murder", "kill", "gore", "blood", "graphic"]})
STEMS["sad"] += ["downer", "unhappy", "bad ending", "ends badly", "tragedy", "bittersweet"]
STEMS["superhero"] += ["marvel"]
STEMS["cheesy"] += ["saccharine", "syrupy"]
STEMS["long"] += ["two hours", "2 hours", "120", "hours"]
GENRE_OF.update({"adventure": "adventure", "mystery": "mystery", "western": "western", "documentary": "documentary", "family": "family", "horror": "horror"})
