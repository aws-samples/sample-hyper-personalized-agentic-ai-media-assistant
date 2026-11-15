T, F, C = "taste", "fanout", "catch_all"
ANY = (T, F, C)

CASES = [
    # --- refinements: funnier ---
    dict(
        msg="something funnier",
        who="R",
        hist=["looking for a comedy tonight"],
        want=["comedy"],
        paths=(F,),
    ),
    # --- refinement: older ---
    dict(
        msg="something a bit older",
        who="A",
        hist=["recommend a good action movie"],
        want=["action"],
        paths=(F,),
    ),
    # --- refinement: shorter ---
    dict(
        msg="can you make it shorter",
        who="R",
        hist=["I want a romance", "something sweet"],
        want=["romance"],
        soft=["long"],
        paths=(F,),
    ),
    # --- carry format forward: TV ---
    dict(
        msg="actually make it funnier",
        who="R",
        hist=["find me a comedy TV show"],
        want=["comedy"],
        fields=dict(category="tv"),
        paths=(C,),
    ),
    # --- carry format forward: micro dramas ---
    dict(
        msg="give me another one like that",
        who="A",
        hist=["show me some micro dramas", "something with action"],
        want=["micro", "action"],
        fields=dict(category="micro-drama"),
        paths=(C,),
    ),
    # --- carry avoid forward: no horror then 'more like that' ---
    dict(
        msg="more like that",
        who="R",
        hist=["a thriller but no horror please", "that was good"],
        want=["thriller"],
        avoid=["horror"],
        hard=["horror"],
        paths=(C,),
    ),
    # --- reversal: actually horror is fine ---
    dict(
        msg="actually horror is fine now",
        who="A",
        hist=["something scary but no horror", "hmm"],
        want=["scary", "horror"],
        notavoid=["horror"],
        paths=(F,),
    ),
    # --- dropping a constraint: forget the 90s thing ---
    dict(
        msg="forget the 90s thing, just a good action film",
        who="A",
        hist=["an action movie from the 90s"],
        want=["action"],
        paths=(F,),
    ),
    # --- switching topic entirely: a documentary ---
    dict(
        msg="ok totally different, a documentary",
        who="A",
        hist=["recommend a fun comedy"],
        want=["documentary"],
        notwant=["comedy"],
        paths=(F,),
    ),
    # --- adding a second viewer who hates gore ---
    dict(
        msg="my partner just joined, she hates gore",
        who="A",
        hist=["an intense action thriller"],
        want=["action", "thriller"],
        avoid=["gore"],
        paths=(F,),
    ),
    # --- open follow-up: you pick ---
    dict(
        msg="you pick",
        who="R",
        hist=["I don't know what I want tonight"],
        leak=True,
        paths=(T,),
    ),
    # --- open follow-up: anything else? ---
    dict(
        msg="anything else?",
        who="A",
        hist=["give me a sci-fi movie", "seen that one"],
        want=["scifi"],
        paths=(F,),
    ),
    # --- reference-like follow-up: more like the second one ---
    dict(
        msg="more like the second one",
        who="R",
        hist=["suggest a few rom-coms"],
        paths=ANY,
    ),
    # --- reference-like follow-up: something with that actor ---
    dict(
        msg="something with that actor",
        who="A",
        hist=["that Nolan movie about dreams"],
        paths=ANY,
    ),
    # --- stacking refinements over turns ---
    dict(
        msg="and shorter too",
        who="R",
        hist=["a comedy", "something funnier", "a bit older"],
        want=["comedy"],
        soft=["long"],
        paths=(F,),
    ),
    # --- correction of mis-heard voice request ---
    dict(
        msg="no I said thriller not filler",
        who="A",
        hist=["find me a filler"],
        want=["thriller"],
        paths=(F,),
    ),
    # --- refinement: scarier ---
    dict(
        msg="make it scarier",
        who="A",
        hist=["a horror movie for tonight"],
        want=["horror", "scary"],
        paths=(F,),
    ),
    # --- refinement: more romantic ---
    dict(
        msg="more romantic please",
        who="R",
        hist=["a feel-good family film"],
        want=["family", "romance"],
        paths=(F,),
    ),
    # --- carry format forward: TV, add genre ---
    dict(
        msg="now a crime one",
        who="A",
        hist=["recommend a drama series", "loved it"],
        want=["crime"],
        fields=dict(category="tv"),
        paths=(C,),
    ),
    # --- reversal: bring back a dropped avoid ---
    dict(
        msg="wait no, gore is actually okay",
        who="A",
        hist=["an action movie but no gore"],
        want=["action"],
        notavoid=["gore"],
        paths=(F,),
    ),
    # --- switch topic: from action to romance ---
    dict(
        msg="eh, give me a romance instead",
        who="R",
        hist=["something with lots of action"],
        want=["romance"],
        notwant=["action"],
        paths=(F,),
    ),
    # --- dropping avoid, keeping genre ---
    dict(
        msg="forget the no-subtitles rule, just a good drama",
        who="A",
        hist=["a drama but nothing with subtitles"],
        want=["drama"],
        notavoid=["subtitles"],
        paths=(F,),
    ),
    # --- add second viewer: no scary ---
    dict(
        msg="my kid is watching now so nothing scary",
        who="R",
        hist=["an adventure movie"],
        want=["adventure"],
        avoid=["scary"],
        paths=(F,),
    ),
    # --- open follow-up with no stated genre ever ---
    dict(
        msg="surprise me",
        who="A",
        hist=["not sure what to watch"],
        leak=True,
        paths=(T,),
    ),
    # --- carry avoid forward: no violence, more like that ---
    dict(
        msg="more like that one",
        who="R",
        hist=["a comedy with no violence", "nice"],
        want=["comedy"],
        avoid=["violence"],
        paths=(F,),
    ),
    # --- reference-only follow-up ---
    dict(
        msg="the first one but newer",
        who="A",
        hist=["list some sci-fi classics"],
        paths=ANY,
    ),
    # --- correction (typo) ---
    dict(
        msg="no not comdey, i ment a crime drama",
        who="A",
        hist=["recommend a comedy"],
        want=["crime", "drama"],
        notwant=["comedy"],
        paths=(F,),
    ),
    # --- refinement: less cheesy ---
    dict(
        msg="something less cheesy",
        who="R",
        hist=["a romance movie"],
        want=["romance"],
        soft=["cheesy"],
        paths=(F,),
    ),
    # --- stacking: genre + era + person over turns ---
    dict(
        msg="and from the 90s",
        who="A",
        hist=["a Nolan thriller", "actually any director is fine"],
        want=["thriller"],
        fields=dict(min_year=1990, max_year=1999),
        paths=(C,),
    ),
    # --- switch to kids content ---
    dict(
        msg="actually something for the kids",
        who="R",
        hist=["a steamy romance"],
        want=["family"],
        notwant=["romance"],
        fields=dict(kids=True),
        paths=(C,),
    ),
    # --- carry forward: micro dramas, funnier ---
    dict(
        msg="got any funnier ones",
        who="R",
        hist=["show me micro dramas"],
        want=["comedy", "micro"],
        fields=dict(category="micro-drama"),
        paths=(C,),
    ),
    # --- reversal: re-add genre want ---
    dict(
        msg="on second thought keep the action",
        who="A",
        hist=["an action film", "no wait, just a drama"],
        want=["action", "drama"],
        paths=(F,),
    ),
    # --- open: anything, with avoid only ---
    dict(
        msg="whatever you think, just no horror",
        who="R",
        hist=["dunno, something to relax to"],
        avoid=["horror"],
        hard=["horror"],
        leak=True,
        paths=(T,),
    ),
    # --- switch topic to documentary with person ---
    dict(
        msg="totally different, a documentary with hanks",
        who="A",
        hist=["a fun action movie"],
        want=["documentary"],
        notwant=["action"],
        fields=dict(person="hanks"),
        paths=(C,),
    ),
    # --- refinement: more intense (typo) ---
    dict(
        msg="can it be more intesne",
        who="A",
        hist=["a thriller please"],
        want=["thriller", "intense"],
        paths=(F,),
    ),
    # --- highly rated carried refinement ---
    dict(
        msg="only highly rated ones though",
        who="A",
        hist=["a crime drama"],
        want=["crime", "drama"],
        fields=dict(min_rating=8.0),
        paths=(C,),
    ),
    # --- award-winner follow-up ---
    dict(
        msg="preferably award winners",
        who="R",
        hist=["a good romance"],
        want=["romance"],
        fields=dict(awards=True),
        paths=(C,),
    ),
    # --- switch to TV, carry avoid ---
    dict(
        msg="make it a TV series, still no gore",
        who="A",
        hist=["an action thriller, no gore"],
        want=["action", "thriller"],
        avoid=["gore"],
        fields=dict(category="tv"),
        paths=(C,),
    ),
    # --- open follow-up after persona-only start ---
    dict(
        msg="anything you'd recommend",
        who="R",
        hist=["hey", "what's good"],
        leak=True,
        paths=(T,),
    ),
    # --- stacking: drop era, keep genre, add exact-ish title reference ---
    dict(
        msg="forget the recent thing, something more like inception",
        who="A",
        hist=["a recent sci-fi movie"],
        want=["scifi", "inception"],
        paths=(F,),
    ),
]

EXTRA_STEMS = {
    "intense": ["intens", "intesne", "gripping", "nail-biting"],
    "filler": ["filler"],
}

EXTRA_GENRE_OF = {}
