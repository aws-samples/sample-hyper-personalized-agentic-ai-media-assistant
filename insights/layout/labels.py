"""Cluster labels from catalog metadata (pure Python, no numpy).

A cluster is named by the genres that are both common inside it and
over-represented compared with the whole catalog, so "drama" (in almost every
cluster) only wins where nothing more distinctive does.
"""

from collections import Counter

SERIES_TYPES = {"tvSeries", "tvMiniSeries", "tvEpisode"}
TYPE_NAMES = {"micro-drama": "Microdramas", "videoGame": "Games", "short": "Shorts"}


def label_clusters(titles, assignments, min_share=0.25, max_genres=2):
    """titles: [{genres, type}], assignments: [cluster id per title].
    Returns {cluster_id: {"label", "size", "genres": [(genre, share)], "series_share"}}."""
    total = len(titles) or 1
    global_counts = Counter(g for t in titles for g in set(t["genres"]))
    members = {}
    for t, c in zip(titles, assignments):
        members.setdefault(c, []).append(t)

    out = {}
    for c, ts in members.items():
        n = len(ts)
        counts = Counter(g for t in ts for g in set(t["genres"]))
        scored = []
        for g, k in counts.items():
            share = k / n
            lift = share / (global_counts[g] / total)
            if share >= min_share:
                scored.append((share * lift, g, share))
        scored.sort(reverse=True)
        picked = [(g, round(share, 2)) for _, g, share in scored[:max_genres]]
        if not picked and counts:
            g, k = counts.most_common(1)[0]
            picked = [(g, round(k / n, 2))]

        types = Counter(t["type"] for t in ts)
        top_type, top_k = types.most_common(1)[0] if types else ("", 0)
        series_share = sum(types[x] for x in SERIES_TYPES) / n
        name = " · ".join(g.title() for g, _ in picked) or "Mixed"
        if top_type in TYPE_NAMES and top_k / n >= 0.5:
            name = f"{TYPE_NAMES[top_type]} · {name}"
        elif series_share >= 0.6:
            name = f"{name} series"
        out[c] = {"label": name, "size": n, "genres": picked, "series_share": round(series_share, 2),
                  "examples": examples(ts)}
    # Two clusters with the same genres are told apart by their best-known title.
    by_label = Counter(v["label"] for v in out.values())
    for v in out.values():
        if by_label[v["label"]] > 1 and v["examples"]:
            v["label"] = f'{v["label"]} (like {v["examples"][0]})'
    return out


def examples(ts, k=3):
    """Highest-rated named titles in a cluster (episodes have episode names, so skip them)."""
    named = [t for t in ts if t["type"] != "tvEpisode" and t.get("name")]
    named.sort(key=lambda t: -(t.get("rating") or 0))
    return [t["name"] for t in named[:k]]
