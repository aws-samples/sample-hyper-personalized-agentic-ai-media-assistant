"""Worked examples appended to the understanding prompt (understand.py).

Purpose: (1) show the exact output for each kind of request, (2) push the prompt past
Haiku 4.5's 4,096-token minimum so Bedrock can cache it.
Topics deliberately differ from the eval suites in evals/ (pirates, chess, sharks,
westerns, smoking...). Do not copy eval cases in here: it would hide regressions.
"""
import json

_DEFAULT = {
    "category": "movie", "person": None, "exact_title": None, "genres_include": [],
    "genres_exclude": [], "avoid_soft": [], "min_year": None, "max_year": None,
    "min_rating": None, "kids": False, "awards": False, "keywords": [], "mood": "",
    "other_constraints": [], "search_text": "", "open_ended": False, "overrides": [],
    "intent": "recommend",
}

# (rules, earlier requests, latest message, fields that differ from _DEFAULT)
EXAMPLES = [
    ([], [], "pirate adventures",
     dict(genres_include=["adventure"], keywords=["pirates"], search_text="swashbuckling pirate adventure on the high seas")),
    ([], [], "a jazz documentary",
     dict(genres_include=["documentary", "music"], keywords=["jazz"], search_text="documentary about jazz musicians")),
    ([], [], "films directed by Greta Gerwig",
     dict(person="Greta Gerwig", search_text="films directed by Greta Gerwig")),
    ([], [], "start Jaws",
     dict(exact_title="Jaws", search_text="Jaws")),
    ([], [], "something like Jaws but set in space",
     dict(genres_include=["sci-fi", "thriller"], keywords=["space", "creature"], search_text="tense creature thriller set in space, in the spirit of Jaws")),
    ([], [], "is Jaws on here?",
     dict(exact_title="Jaws", search_text="Jaws")),
    ([], [], "top-rated westerns from the 60s",
     dict(genres_include=["western"], min_year=1960, max_year=1969, min_rating=8.0, search_text="classic 1960s western")),
    ([], [], "a cartoon for my 4 year old, nothing loud",
     dict(genres_include=["animation", "family"], kids=True, avoid_soft=["loud, noisy action"], search_text="gentle animated film for young children")),
    ([], [], "pick something for me, just no biopics",
     dict(genres_exclude=["biography"], open_ended=True)),
    ([], [], "up to you, but nothing too slow-paced",
     dict(avoid_soft=["too slow-paced"], open_ended=True)),
    ([], [], "your call",
     dict(open_ended=True)),
    ([], [], "nothing gloomy tonight",
     dict(avoid_soft=["gloomy"], open_ended=True)),
    ([], [], "maybe a western or a heist thing, you choose",
     dict(genres_include=["western"], keywords=["heist"], search_text="western or heist film")),
    ([], [], "I'm bored of courtroom dramas, show me something else",
     dict(avoid_soft=["courtroom dramas"], open_ended=True)),
    ([], [], "I've watched every Pixar film, recommend something new",
     dict(avoid_soft=["Pixar films"], open_ended=True)),
    ([], ["a detective story, no musicals"], "more like that",
     dict(genres_include=["mystery", "crime"], genres_exclude=["musical"], keywords=["detective"], search_text="detective mystery")),
    ([], [], "I'm exhausted, something easy",
     dict(mood="easy, light", search_text="easygoing light film")),
    ([], [], "chess movies that aren't dull",
     dict(keywords=["chess"], avoid_soft=["dull"], search_text="gripping, competitive chess story")),
    ([], [], "a ballet drama that isn't too melodramatic",
     dict(genres_include=["drama"], keywords=["ballet"], avoid_soft=["too melodramatic"], search_text="restrained, grounded ballet drama")),
    ([], [], "vampire flicks, more funny than scary",
     dict(genres_include=["comedy"], keywords=["vampires"], avoid_soft=["too scary"], mood="funny", search_text="funny vampire comedy")),
    ([], [], "a golf movie my dad and I would both enjoy, nothing mean-spirited",
     dict(genres_include=["sport"], keywords=["golf"], avoid_soft=["mean-spirited"], other_constraints=["suits a parent and adult child"], search_text="warm, good-natured golf movie")),
    ([], [], "Korean thrillers, ideally in the original language",
     dict(genres_include=["thriller"], other_constraints=["Korean language"], search_text="Korean thriller")),
    ([], [], "a cooking competition show",
     dict(category="tv", genres_include=["reality-tv", "game-show"], keywords=["cooking"], search_text="cooking competition series")),
    ([], [], "micro dramas about office rivals",
     dict(category="micro-drama", keywords=["office rivalry"], search_text="office rivalry micro drama")),
    ([], [], "a dragon fantasy epic, not a cartoon",
     dict(genres_include=["fantasy", "adventure"], genres_exclude=["animation"], keywords=["dragons"], search_text="epic live-action dragon fantasy")),
    ([], [], "award-winning foreign dramas",
     dict(genres_include=["drama"], awards=True, other_constraints=["not in English"], search_text="acclaimed international drama")),
    ([], [], "a movie with Keanu Reeves, but not the John Wick ones",
     dict(person="Keanu Reeves", avoid_soft=["John Wick films"], search_text="Keanu Reeves film")),
    ([], ["shark movies"], "from before 2000 please",
     dict(keywords=["sharks"], max_year=1999, search_text="classic shark thriller")),
    ([], ["space operas, no comedies"], "actually comedy is okay",
     dict(genres_include=["sci-fi"], keywords=["space opera"], search_text="space opera adventure")),
    ([], ["Bollywood musicals"], "ok, completely different: a courtroom drama",
     dict(genres_include=["drama"], keywords=["courtroom"], search_text="tense courtroom drama")),
    ([], ["a heist film"], "up to you",
     dict(keywords=["heist"], search_text="clever heist film")),
    ([], ["a pirate adventure, nothing too scary"], "more like that",
     dict(genres_include=["adventure"], keywords=["pirates"], avoid_soft=["too scary"], search_text="swashbuckling pirate adventure")),
    (["avoid genre: western", "avoid theme: smoking"], [], "a noir full of chain-smoking detectives",
     dict(genres_include=["crime", "mystery"], keywords=["film noir", "detectives"], search_text="moody film noir detective story", overrides=["avoid theme: smoking"])),
    (["avoid genre: western", "avoid theme: smoking"], [], "a sports comedy",
     dict(genres_include=["sport", "comedy"], search_text="feel-good sports comedy")),
    (["avoid genre: western", "avoid theme: smoking"], ["cowboys, saloons and a showdown at noon"], "another one like that",
     dict(genres_include=["western"], keywords=["cowboys", "showdown"], search_text="classic cowboy showdown western", overrides=["avoid genre: western"])),
    (["avoid genre: western", "avoid theme: smoking"], ["a western"], "fine, something else: a sitcom",
     dict(category="tv", genres_include=["comedy"], search_text="sitcom")),
    (["avoid genre: western", "avoid theme: smoking"], [], "surprise me",
     dict(open_ended=True)),
    (["avoid theme: spiders"], [], "creepy-crawly eight-legged nightmares",
     dict(genres_include=["horror"], keywords=["spiders"], search_text="creature feature about giant spiders", overrides=["avoid theme: spiders"])),
    # intent 'answer': questions about titles already shown (listed under _prev).
    ([], ["pirate adventures"], "which of these is the shortest?",
     dict(_prev=["Pirates of the Caribbean", "Treasure Planet", "Hook"], intent="answer", genres_include=["adventure"], keywords=["pirates"])),
    ([], ["a chess movie"], "who plays the lead in the first one?",
     dict(_prev=["The Queen's Gambit", "Pawn Sacrifice", "Searching for Bobby Fischer"], intent="answer", keywords=["chess"])),
    ([], ["shark movies"], "is the second one okay for a 10 year old?",
     dict(_prev=["Jaws", "Finding Nemo", "The Meg"], intent="answer", keywords=["sharks"])),
    ([], ["shark movies"], "more like the second one",
     dict(_prev=["Jaws", "Finding Nemo", "The Meg"], genres_include=["animation", "family"], keywords=["ocean"], search_text="gentle animated ocean adventure, like Finding Nemo")),
    ([], ["a heist film"], "thanks, that's great",
     dict(_prev=["Ocean's Eleven", "Inside Man"], intent="answer", keywords=["heist"])),
]


def render() -> str:
    out = ["\n\nWORKED EXAMPLES (input -> exact output):"]
    for rules, hist, msg, fields in EXAMPLES:
        fields = dict(fields)
        prev = fields.pop("_prev", [])
        d = dict(_DEFAULT, **fields)
        inp = []
        if hist:
            inp.append("[Earlier requests in this conversation] " + " | ".join(hist))
        if prev:
            inp.append("[Already recommended] " + " | ".join(prev))
        inp.append(f"[Latest message]: {msg}")
        if rules:
            inp.append("[Settings rules] " + " | ".join(rules))
        out.append("Input: " + "  ".join(inp) + "\nOutput: " + json.dumps(d, separators=(",", ":")))
    return "\n".join(out) + "\n"
