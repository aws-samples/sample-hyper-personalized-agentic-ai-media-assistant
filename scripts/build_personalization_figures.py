"""Build the figures for docs/how-personalization-works.md from the real constants.

    python scripts/build_personalization_figures.py          # write docs/figures/*.svg
    python scripts/build_personalization_figures.py --check  # exit 1 if a file is stale

The half-lives drawn are read from the production code, so the picture cannot
drift from it. infra/deployment/test_personalization_doc.py
runs the --check comparison in the deploy test suite.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
sys.dont_write_bytecode = True

import l2  # noqa: E402  (pure module: constants + update functions)

OUT = ROOT / "docs/figures"
LISTED_FLOOR = 0.3          # backend/movie-ai-agent-v2/user_profile.py render_affinities
FONT = 'font-family="Helvetica, Arial, sans-serif"'
INK, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"
BLUE, ORANGE, RED = "#2563eb", "#ea580c", "#dc2626"


def _f(x):
    return f"{x:.1f}"


def fade_curves():
    """Entity score fade: s_now / s against days since `last`, per half-life."""
    w, h, left, right, top, bottom = 700, 330, 60, 200, 40, 50
    days = 120
    pw, ph = w - left - right, h - top - bottom

    def px(d):
        return left + pw * d / days

    def py(v):
        return top + ph * (1 - v)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {FONT}>',
             f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
             f'<text x="{left}" y="22" font-size="15" font-weight="bold" fill="{INK}">'
             'How a favorite fades when you stop watching it</text>']
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line x1="{left}" y1="{_f(py(v))}" x2="{left + pw}" y2="{_f(py(v))}" stroke="{GRID}"/>')
        parts.append(f'<text x="{left - 8}" y="{_f(py(v) + 4)}" font-size="11" text-anchor="end" fill="{MUTED}">{v:g}</text>')
    for d in range(0, days + 1, 30):
        parts.append(f'<text x="{_f(px(d))}" y="{top + ph + 18}" font-size="11" text-anchor="middle" fill="{MUTED}">{d}</text>')
    parts.append(f'<text x="{left + pw / 2}" y="{h - 8}" font-size="12" text-anchor="middle" fill="{INK}">'
                 'days since you last watched it</text>')
    parts.append(f'<text x="16" y="{top + ph / 2}" font-size="12" fill="{INK}" text-anchor="middle" '
                 f'transform="rotate(-90 16 {top + ph / 2})">share of the stored score left</text>')
    # The listing floor applies to a score that started at 1.0; drawn as a reference line.
    parts.append(f'<line x1="{left}" y1="{_f(py(LISTED_FLOOR))}" x2="{left + pw}" y2="{_f(py(LISTED_FLOOR))}" '
                 f'stroke="{RED}" stroke-dasharray="5 4"/>')
    parts.append(f'<text x="{left + 6}" y="{_f(py(LISTED_FLOOR) - 6)}" font-size="11" fill="{RED}">'
                 'below this line, search stops mentioning it (for a top score of 1.0)</text>')
    for kind, label, colour in (("genre", "genre", BLUE), ("person", "actor", ORANGE)):
        half = l2.HALF_LIFE_DAYS[kind]
        pts = " ".join(f"{_f(px(d))},{_f(py(0.5 ** (d / half)))}" for d in range(0, days + 1, 2))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2.5"/>')
        end = 0.5 ** (days / half)
        parts.append(f'<text x="{left + pw + 6}" y="{_f(py(end) + 4)}" font-size="12" fill="{colour}">'
                     f'{label} (halves every {half} days)</text>')
        for k in (1, 2):
            d = half * k
            if d < days:
                parts.append(f'<circle cx="{_f(px(d))}" cy="{_f(py(0.5 ** k))}" r="4" fill="{colour}"/>')
                parts.append(f'<text x="{_f(px(d) + 6)}" y="{_f(py(0.5 ** k) - 6)}" font-size="11" fill="{colour}">'
                             f'{0.5 ** k:g}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


FIGURES = {"fade-curves.svg": fade_curves}


def main(argv):
    check = "--check" in argv
    stale = []
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in FIGURES.items():
        path, svg = OUT / name, build()
        if check:
            if not path.exists() or path.read_text() != svg:
                stale.append(name)
        else:
            path.write_text(svg)
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("stale figures (run scripts/build_personalization_figures.py): " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
