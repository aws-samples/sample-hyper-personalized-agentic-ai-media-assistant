T, F, C = "taste", "fanout", "catch_all"
ANY = (T, F, C)

CASES = [
    # 1 - multi-constraint: genre + era + person + avoid
    dict(
        msg="I want a smart sci-fi thriller from the 90s directed by someone like Nolan, nothing with gore please",
        who="A",
        paths=("catch_all",),
        want=("scifi", "thriller", "cerebral"),
        avoid=("gore",),
        hard=("gore",),
        fields=dict(min_year=1990, max_year=1999),
    ),
    # 2 - multiple viewers, different tastes, plus avoid
    dict(
        msg="My teenager wants action, I want something smart, and absolutely no gore for either of us",
        who="A",
        paths=("fanout",),
        want=("action", "cerebral"),
        avoid=("gore",),
        hard=("gore",),
    ),
    # 3 - occasion date night, hedged
    dict(
        msg="Date night tonight, something romantic but funny, not too cheesy though",
        who="R",
        paths=("fanout",),
        want=("romance", "comedy"),
        soft=("cheesy",),
    ),
    # 4 - rainy sunday indirect mood
    dict(
        msg="It's a rainy Sunday and I just want something cozy and warm to curl up with, family friendly",
        who="R",
        paths=("fanout",),
        want=("family",),
    ),
    # 5 - long flight runtime requirement (no simple field)
    dict(
        msg="I've got a long flight coming up so I need something really long and engrossing, an epic adventure maybe",
        who="A",
        paths=("fanout",),
        want=("long", "adventure"),
    ),
    # 6 - with grandparents, avoid steamy/violence
    dict(
        msg="Watching with my grandparents this weekend, keep it gentle, nothing steamy or violent",
        who="R",
        paths=("taste",),
        avoid=("steamy", "violence"),
        hard=("violence",),
        leak=True,
    ),
    # 7 - under 2 hours requirement
    dict(
        msg="Something under 2 hours, a tight little thriller I can finish before bed",
        who="A",
        paths=("fanout",),
        want=("short", "thriller"),
    ),
    # 8 - in Spanish, requirement no simple field + true story
    dict(
        msg="A drama in Spanish based on a true story would be perfect, something moving",
        who="R",
        paths=("fanout",),
        want=("spanish", "truestory", "drama"),
    ),
    # 9 - for a 10 year old
    dict(
        msg="Need a fun movie for my 10 year old, animated adventure, nothing scary",
        who="R",
        paths=("catch_all",),
        want=("animation", "adventure"),
        avoid=("scary",),
        soft=("scary",),
        fields=dict(kids=True),
    ),
    # 10 - half-watch while cooking
    dict(
        msg="I want something light I can half-watch while I'm cooking, a dumb comedy is fine",
        who="R",
        paths=("fanout",),
        want=("comedy", "dumb"),
    ),
    # 11 - contradictory scary but not too scary
    dict(
        msg="A horror movie that's scary but not too scary, you know what I mean?",
        who="A",
        paths=("fanout",),
        want=("horror", "scary"),
        soft=("scary",),
    ),
    # 12 - hedged funny or maybe a thriller
    dict(
        msg="Honestly not sure, something funny or maybe a thriller, surprise me",
        who="A",
        paths=("fanout",),
        want=("comedy", "thriller"),
    ),
    # 13 - rambling voice transcript with filler
    dict(
        msg="ok so um i had kind of a weird week and i was thinking like maybe we could watch something you know not too heavy but also not totally brainless like a good crime drama or something with a bit of mystery to it if that makes sense",
        who="A",
        paths=("fanout",),
        want=("crime", "drama", "mystery"),
    ),
    # 14 - comparative like Knives Out but darker
    dict(
        msg="Something like Knives Out but darker, more twisted",
        who="A",
        paths=("fanout",),
        want=("mystery", "dark"),
    ),
    # 15 - indirect mood terrible day
    dict(
        msg="I had a terrible day, I just need something to make me laugh",
        who="R",
        paths=("fanout",),
        want=("comedy",),
    ),
    # 16 - person + genre + avoid, with typos
    dict(
        msg="anythng with tom hanks thats uplifting, no war stuff plz",
        who="R",
        paths=("catch_all",),
        want=("hanks",),
        avoid=("war",),
        hard=("war",),
        fields=dict(person="hanks"),
    ),
    # 17 - highly rated + era + genre
    dict(
        msg="A highly rated action movie from the 2000s, something intense",
        who="A",
        paths=("catch_all",),
        want=("action", "intense"),
        fields=dict(min_year=2000, max_year=2009, min_rating=8.0),
    ),
    # 18 - award winner + drama
    dict(
        msg="Give me an award winning drama, something that really swept the Oscars",
        who="R",
        paths=("catch_all",),
        want=("drama",),
        fields=dict(awards=True),
    ),
    # 19 - exact title comparative
    dict(
        msg="I loved Inception, I want something just as mind bending and confusing in a good way",
        who="A",
        paths=("fanout",),
        want=("cerebral", "confusing"),
    ),
    # 20 - multiple viewers kids + adult, avoid
    dict(
        msg="Movie night with the kids and grandma, something everyone can enjoy, no violence and nothing too scary",
        who="R",
        paths=("catch_all",),
        want=("family",),
        avoid=("violence", "scary"),
        hard=("violence",),
        soft=("scary",),
        fields=dict(kids=True),
    ),
    # 21 - long rambling with occasion
    dict(
        msg="so my partner and i finally have a free evening and we wanna do like a proper date night thing with wine and all that, we're thinking a romance but like a smart one not a sappy predictable one you know",
        who="R",
        paths=("fanout",),
        want=("romance", "cerebral"),
        soft=("cheesy",),
    ),
    # 22 - true story + sport
    dict(
        msg="A sports movie based on a true story, something inspiring about an underdog",
        who="A",
        paths=("fanout",),
        want=("sport", "truestory"),
    ),
    # 23 - foreign subtitles ok, genre
    dict(
        msg="I don't mind subtitles, give me a tense foreign crime thriller",
        who="A",
        paths=("fanout",),
        want=("subtitles", "crime", "thriller"),
    ),
    # 24 - hard exclusion + positive want
    dict(
        msg="Something adventurous and fun, but absolutely no horror, I can't do horror",
        who="A",
        paths=("catch_all",),
        want=("adventure",),
        avoid=("horror",),
        hard=("horror",),
    ),
    # 25 - comparative darker + person
    dict(
        msg="Like a Nolan film but with more heart, something cerebral yet emotional",
        who="A",
        paths=("catch_all",),
        want=("nolan", "cerebral"),
        fields=dict(person="nolan"),
    ),
    # 26 - reversal / notavoid
    dict(
        msg="I said no romance earlier but actually a little romance is fine, pair it with a comedy",
        who="R",
        paths=("fanout",),
        hist=["No romance please, just a comedy"],
        want=("comedy", "romance"),
        notavoid=("romance",),
    ),
    # 27 - half watch + runtime
    dict(
        msg="Something short and silly I can put on in the background while I fold laundry, slapstick works",
        who="R",
        paths=("fanout",),
        want=("short", "slapstick", "comedy"),
    ),
    # 28 - micro-drama (catch_all category)
    dict(
        msg="I'm into those short vertical micro dramas lately, got any addictive ones?",
        who="R",
        paths=("catch_all",),
        want=("micro",),
        fields=dict(category="micro-drama"),
    ),
    # 29 - TV series request
    dict(
        msg="Not a movie, I want a bingeable TV crime series to get hooked on",
        who="A",
        paths=("catch_all",),
        want=("crime",),
        fields=dict(category="tv"),
    ),
    # 30 - hedged scary + occasion halloween
    dict(
        msg="It's almost Halloween, something spooky and fun but not gory, we've got younger cousins over",
        who="A",
        paths=("catch_all",),
        want=("scary",),
        avoid=("gore",),
        hard=("gore",),
        soft=("scary",),
        fields=dict(kids=True),
    ),
    # 31 - long voice transcript multiple constraints
    dict(
        msg="umm okay so basically i want something that's got like action and maybe a bit of sci-fi vibe but also it should kinda make you think, oh and please nothing super long i'm tired, and no sad endings i cant handle sad tonight",
        who="A",
        paths=("fanout",),
        want=("action", "scifi", "cerebral", "short"),
        avoid=("sad",),
        soft=("sad",),
    ),
    # 32 - comparative + exact-ish title + darker
    dict(
        msg="Something in the vein of The Godfather, a sprawling crime epic, the darker and more serious the better",
        who="A",
        paths=("fanout",),
        want=("crime", "dark", "long"),
    ),
    # 33 - indirect mood + genre pivot
    dict(
        msg="Feeling nostalgic, something warm from the 80s, maybe a feel good adventure",
        who="R",
        paths=("catch_all",),
        want=("adventure",),
        fields=dict(min_year=1980, max_year=1989),
    ),
    # 34 - multiple viewers conflicting with avoid, typos
    dict(
        msg="my wife wants a romcom and i want a thriller, can we find somthing that works for both, no gore though she hates it",
        who="A",
        paths=("fanout",),
        want=("romance", "comedy", "thriller"),
        avoid=("gore",),
        hard=("gore",),
    ),
    # 35 - documentary true story
    dict(
        msg="A gripping documentary about real events, true crime maybe",
        who="A",
        paths=("fanout",),
        want=("documentary", "truestory", "crime"),
    ),
    # 36 - avoid only (taste route), leak guard
    dict(
        msg="Just put on anything, honestly, as long as it isn't scary or sad",
        who="A",
        paths=("taste",),
        avoid=("scary", "sad"),
        soft=("scary", "sad"),
        leak=True,
    ),
    # 37 - highly rated + language + runtime
    dict(
        msg="A highly rated film in French, under two hours, something romantic",
        who="R",
        paths=("catch_all",),
        want=("french", "short", "romance"),
        fields=dict(min_rating=8.0),
    ),
    # 38 - occasion with grandparents + era
    dict(
        msg="Classic movie night with grandma, a lovely old musical from the 50s or 60s",
        who="R",
        paths=("catch_all",),
        want=("musical",),
        fields=dict(min_year=1950, max_year=1969),
    ),
    # 39 - comparative + contradictory hedge
    dict(
        msg="Something like Mad Max, high octane action, but maybe a little less intense if that's possible",
        who="A",
        paths=("fanout",),
        want=("action",),
        soft=("intense",),
    ),
    # 40 - rambling, multiple viewers, kids + runtime, true story
    dict(
        msg="ok so the whole family's here, the kids are 8 and 11, grandpa falls asleep after an hour so nothing too long, something uplifting ideally based on a true story and definitely no violence",
        who="R",
        paths=("catch_all",),
        want=("truestory", "short"),
        avoid=("violence",),
        hard=("violence",),
        fields=dict(kids=True),
    ),
]

EXTRA_STEMS = {
    "truestory": ["true story", "real events", "based on", "biograph", "true crime", "real life"],
    "spanish": ["in spanish", "spanish language", "spanish-language"],
    "french": ["in french", "french language", "french-language"],
    "cerebral": ["mind bend", "makes you think", "make you think", "cerebral", "thought-provoking", "smart", "intelligent"],
    "short": ["under 2 hour", "under two hour", "short", "less than two hour", "not too long", "nothing too long", "quick", "before bed"],
}

EXTRA_GENRE_OF = {
    "truestory": "biography",
    "documentary": "documentary",
}
