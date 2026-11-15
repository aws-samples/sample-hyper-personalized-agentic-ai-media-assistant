T, F, C = "taste", "fanout", "catch_all"
ANY = (T, F, C)

# FAMILY = negation
# 40 cases about avoids / negation for a movie-recommendation query-understanding step.
# who: 'A' = Action Fan (action, sci-fi, drama, Nolan); 'R' = Rom-Com Lover (comedy, romance, family).
#
# Route rules applied:
#  - taste    = viewer states NO positive preference (avoids alone still count as taste)
#  - catch_all= request has a hard filter: person, year/era, rating, kids, awards, exact title,
#               a hard genre exclusion combined with a positive want, micro-drama, or TV
#  - fanout   = positive genre/mood/theme content with no hard filter

CASES = [
    # --- hard genre exclusions, avoid-only (no positive want) -> taste ---
    dict(msg="no horror please", who='R', paths=(T,),
         avoid=("horror",), hard=("horror",), leak=True),
    dict(msg="anything except documentaries", who='A', paths=(T,),
         avoid=("documentary",), hard=("documentary",), leak=True),
    dict(msg="I can't stand musicals", who='R', paths=(T,),
         avoid=("musical",), hard=("musical",), leak=True),
    dict(msg="just no westerns", who='A', paths=(T,),
         avoid=("western",), hard=("western",), leak=True),

    # --- hard genre exclusion + positive want -> catch_all ---
    dict(msg="comedy but not crude", who='R', paths=(C,),
         want=("comedy",), avoid=("raunchy",), soft=("raunchy",)),
    dict(msg="give me a thriller, no horror though", who='A', paths=(C,),
         want=("thriller",), avoid=("horror",), hard=("horror",)),
    dict(msg="something romantic but absolutely no musicals", who='R', paths=(C,),
         want=("romance",), avoid=("musical",), hard=("musical",)),
    dict(msg="action movie but not a war film", who='A', paths=(C,),
         want=("action",), avoid=("war",), hard=("war",)),
    dict(msg="family night pick, nothing animated", who='R', paths=(C,),
         want=("family",), avoid=("animation",), hard=("animation",)),

    # --- soft / degree avoids -> fanout or taste depending on positive content ---
    dict(msg="not too long", who='A', paths=(T,),
         avoid=("long",), soft=("long",), leak=True),
    dict(msg="nothing too scary", who='R', paths=(T,),
         avoid=("scary",), soft=("scary",), leak=True),
    dict(msg="easy on the swearing", who='R', paths=(T,),
         avoid=("raunchy",), soft=("raunchy",), leak=True),
    dict(msg="a comedy that isn't too dumb", who='R', paths=(F,),
         want=("comedy",), avoid=("dumb",), soft=("dumb",)),
    dict(msg="sci-fi but nothing too confusing", who='A', paths=(F,),
         want=("scifi",), avoid=("confusing",), soft=("confusing",)),
    dict(msg="a romance thats not too cheesy", who='R', paths=(F,),
         want=("romance",), avoid=("cheesy",), soft=("cheesy",)),
    dict(msg="drama but not too sad", who='A', paths=(F,),
         want=("drama",), avoid=("sad",), soft=("sad",)),
    dict(msg="an action flick but go easy on the gore", who='A', paths=(F,),
         want=("action",), avoid=("gore",), soft=("gore",)),

    # --- non-genre avoids: person, franchise, theme, subtitles, b&w, sequels ---
    dict(msg="anything but an Adam Sandler movie", who='R', paths=(T,),
         avoid=("sandler",), leak=True),
    dict(msg="no more Bond films please", who='A', paths=(T,),
         avoid=("bond",), leak=True),
    dict(msg="nothing about cancer", who='R', paths=(T,),
         avoid=("cancer",), leak=True),
    dict(msg="i hate reading subtitles, skip those", who='A', paths=(T,),
         avoid=("subtitles",), leak=True),
    dict(msg="no black and white movies", who='R', paths=(T,),
         avoid=("blackandwhite",), leak=True),
    dict(msg="please no sequels", who='A', paths=(T,),
         avoid=("sequel",), leak=True),
    dict(msg="skip the whole marvel superhero thing", who='A', paths=(T,),
         avoid=("superhero",), leak=True),
    dict(msg="comedy but not an Adam Sandler one", who='R', paths=(C,),
         want=("comedy",), avoid=("sandler",), fields=dict(person="sandler")),

    # --- open requests with only an avoid ('surprise me but no X') -> taste ---
    dict(msg="surprise me but no horror", who='R', paths=(T,),
         avoid=("horror",), hard=("horror",), leak=True),
    dict(msg="pick anything, just not a zombie movie", who='A', paths=(T,),
         avoid=("zombie",), leak=True),
    dict(msg="whatever's good tonight, nothing sad though", who='A', paths=(T,),
         avoid=("sad",), soft=("sad",), leak=True),
    dict(msg="dealer's choice, spare me the romance", who='A', paths=(T,),
         avoid=("romance",), hard=("romance",), leak=True),

    # --- double / multiple avoids ---
    dict(msg="no horror and nothing too long", who='R', paths=(T,),
         avoid=("horror", "long"), hard=("horror",), soft=("long",), leak=True),
    dict(msg="not scary, not sad, not three hours long", who='A', paths=(T,),
         avoid=("scary", "sad", "long"), soft=("scary", "sad", "long"), leak=True),
    dict(msg="a comedy but no gross-out humor and no musicals", who='R', paths=(C,),
         want=("comedy",), avoid=("raunchy", "musical"),
         hard=("musical",), soft=("raunchy",)),

    # --- avoids phrased indirectly ---
    dict(msg="i'm sick of superhero movies", who='A', paths=(T,),
         avoid=("superhero",), leak=True),
    dict(msg="rom-coms are off the table", who='R', paths=(T,),
         avoid=("romance",), hard=("romance",), leak=True),
    dict(msg="spare me the jump scares", who='R', paths=(T,),
         avoid=("scary",), soft=("scary",), leak=True),
    dict(msg="anything but another war movie", who='A', paths=(T,),
         avoid=("war",), hard=("war",), leak=True),

    # --- double negatives ---
    dict(msg="not something that isn't funny", who='R', paths=(F,),
         want=("comedy",)),
    dict(msg="i don't want anything that's not at least a little scary", who='A', paths=(F,),
         want=("scary",), notavoid=("scary",)),

    # --- avoids of things the persona normally likes ---
    dict(msg="no sci-fi tonight for once", who='A', paths=(T,),
         avoid=("scifi",), hard=("scifi",), notwant=("scifi",), leak=True),
    dict(msg="honestly skip the Nolan stuff, too much", who='A', paths=(T,),
         avoid=("nolan",), leak=True),
]

# New concepts introduced above that are not in the provided concept list.
EXTRA_STEMS = {
    "cancer": ["cancer", "terminal illness", "dying of", "tumor", "chemo"],
    "blackandwhite": ["black and white", "black-and-white", "b&w", "b and w", "monochrome"],
    "sequel": ["sequel", "part 2", "part two", "part 3", "franchise", "another one", "follow-up", "followup"],
}

# None of the new concepts above are catalog genres (they are themes/qualities/formats),
# so no new GENRE_OF entries are required.
EXTRA_GENRE_OF = {}
