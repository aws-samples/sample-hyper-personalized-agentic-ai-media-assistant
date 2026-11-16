T, F, C = "taste", "fanout", "catch_all"
ANY = (T, F, C)

CASES = [
    # 1 - single genre, terse (A)
    dict(msg="action movies", who='A', paths=(F,), want=["action"]),
    # 2 - single genre, casual (R)
    dict(msg="I'm in the mood for a rom-com tonight", who='R', paths=(F,),
         want=["romance", "comedy"]),
    # 3 - two genres (A)
    dict(msg="sci-fi thriller please", who='A', paths=(F,),
         want=["scifi", "thriller"]),
    # 4 - two genres, casual (R)
    dict(msg="got any good comedy drama stuff", who='R', paths=(F,),
         want=["comedy", "drama"]),
    # 5 - mood/vibe (R)
    dict(msg="something cozy", who='R', paths=(F,), want=["cozy"]),
    # 6 - mood/vibe (A)
    dict(msg="give me something intense", who='A', paths=(F,), want=["intense"]),
    # 7 - theme: heist (A)
    dict(msg="a good heist movie", who='A', paths=(F,), want=["heist"]),
    # 8 - theme: time travel (A)
    dict(msg="anything with time travel", who='A', paths=(F,), want=["timetravel"]),
    # 9 - theme: survival (R)
    dict(msg="survival movies sound good right now", who='R', paths=(F,),
         want=["survival"]),
    # 10 - person: director Nolan (A) -> catch_all (person filter)
    dict(msg="show me Christopher Nolan films", who='A', paths=(C,),
         want=["nolan"], fields=dict(person="nolan")),
    # 11 - person: actor Tom Hanks (R) -> catch_all
    dict(msg="I want a Tom Hanks movie", who='R', paths=(C,),
         want=["hanks"], fields=dict(person="hanks")),
    # 12 - person: Adam Sandler (R) -> catch_all
    dict(msg="put on an adam sandler comedy", who='R', paths=(C,),
         want=["sandler", "comedy"], fields=dict(person="sandler")),
    # 13 - era: the 90s (A) -> catch_all (year filter)
    dict(msg="action flicks from the 90s", who='A', paths=(C,),
         want=["action"], fields=dict(min_year=1990, max_year=1999)),
    # 14 - decade: 80s (R) -> catch_all
    dict(msg="um like a 80s romance kinda thing", who='R', paths=(C,),
         want=["romance"], fields=dict(min_year=1980, max_year=1989)),
    # 15 - recent (A) -> fanout (vague recent, no year)
    dict(msg="something recent and action packed", who='A', paths=(F,),
         want=["action"]),
    # 16 - highly rated (A) -> catch_all (rating filter)
    dict(msg="only highly rated thrillers", who='A', paths=(C,),
         want=["thriller"], fields=dict(min_rating=8.0)),
    # 17 - kids/family-safe (R) -> catch_all (kids filter)
    dict(msg="something family friendly for the kids", who='R', paths=(C,),
         want=["family"], fields=dict(kids=True)),
    # 18 - kids animation (R) -> catch_all
    dict(msg="a kids cartoon please", who='R', paths=(C,),
         want=["animation"], fields=dict(kids=True)),
    # 19 - award-winning (A) -> catch_all (awards filter)
    dict(msg="award winning dramas", who='A', paths=(C,),
         want=["drama"], fields=dict(awards=True)),
    # 20 - acclaimed (R) -> catch_all (awards)
    dict(msg="show me some critically acclaimed comedies", who='R', paths=(C,),
         want=["comedy"], fields=dict(awards=True)),
    # 21 - exact title (A) -> catch_all
    dict(msg="play Inception", who='A', paths=(C,),
         want=["inception"], fields=dict(exact_title="inception")),
    # 22 - exact title (R) -> catch_all
    dict(msg="can you put on Forrest Gump", who='R', paths=(C,),
         fields=dict(exact_title="forrest gump")),
    # 23 - something like <title> (A) -> fanout
    dict(msg="something like Inception", who='A', paths=(F,), want=["inception"]),
    # 24 - something like <title> (R) -> fanout
    dict(msg="movies like notting hill", who='R', paths=(F,)),
    # 25 - TV series (A) -> catch_all (category tv)
    dict(msg="recommend a good crime tv series", who='A', paths=(C,),
         want=["crime"], fields=dict(category="tv")),
    # 26 - TV series (R) -> catch_all
    dict(msg="I feel like binging a comedy show", who='R', paths=(C,),
         want=["comedy"], fields=dict(category="tv")),
    # 27 - micro drama (R) -> catch_all (category micro-drama)
    dict(msg="show me some micro dramas", who='R', paths=(C,),
         want=["micro"], fields=dict(category="micro-drama")),
    # 28 - micro drama romantic (R) -> catch_all
    dict(msg="a quick romantic micro drama", who='R', paths=(C,),
         want=["micro", "romance"], fields=dict(category="micro-drama")),
    # 29 - plain open: surprise me (A) -> taste (no preference, leak)
    dict(msg="surprise me", who='A', paths=(T,), leak=True),
    # 30 - plain open: what's good tonight (R) -> taste, leak
    dict(msg="whats good tonight", who='R', paths=(T,), leak=True),
    # 31 - plain open: you pick (A) -> taste, leak
    dict(msg="um I dont know you pick", who='A', paths=(T,), leak=True),
    # 32 - single genre voice transcript (A)
    dict(msg="uh yeah just put on like a horror movie or something",
         who='A', paths=(F,), want=["horror"]),
    # 33 - theme: zombie (A)
    dict(msg="zombie flicks", who='A', paths=(F,), want=["zombie"]),
    # 34 - genre with typo (R)
    dict(msg="any good romnce comedis", who='R', paths=(F,),
         want=["romance", "comedy"]),
    # 35 - genre with typo (A)
    dict(msg="scify advnture movie", who='A', paths=(F,),
         want=["scifi", "adventure"]),
    # 36 - person director + genre, voice style (A) -> catch_all
    dict(msg="um something by quentin tarantino you know crime stuff",
         who='A', paths=(C,), want=["crime"], fields=dict(person="tarantino")),
    # 37 - mood/vibe: feel-good (R)
    dict(msg="just want something feel good and light", who='R', paths=(F,),
         want=["feelgood"]),
    # 38 - theme: spy/bond (A)
    dict(msg="a classic spy thriller", who='A', paths=(F,),
         want=["spy", "thriller"]),
    # 39 - documentary (A) -> fanout (genre, no hard filter)
    dict(msg="a nature documentary", who='A', paths=(F,), want=["documentary"]),
    # 40 - two genres casual (R)
    dict(msg="maybe a musical or a romance idk", who='R', paths=(F,),
         want=["musical", "romance"]),
]

EXTRA_STEMS = {
    "cozy": ["cozy", "cosy", "comforting", "warm", "snug"],
    "heist": ["heist", "robbery", "con ", "caper", "steal the"],
    "timetravel": ["time travel", "time-travel", "time machine", "back in time",
                   "temporal"],
    "survival": ["survival", "survive", "stranded", "wilderness", "castaway"],
    "feelgood": ["feel good", "feel-good", "feelgood", "uplifting", "heartwarming",
                 "light"],
    "tarantino": ["tarantino", "quentin"],
}

EXTRA_GENRE_OF = {
    # none of the new concepts are catalog genres (they are themes/moods/people)
}
