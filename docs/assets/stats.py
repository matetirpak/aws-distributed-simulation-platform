"""The README's stats panel, as SVG in GitHub's two themes. Edit the numbers and run:

  python3 docs/assets/stats.py

Measured in the batch in the README's Results section.
"""

from pathlib import Path
from xml.sax.saxutils import escape

HEADLINE = [  # (number, caption)
    ("1,000", "matches completed in one batch"),
    ("2,000", "bots, each on its own EC2 machine"),
    ("0", "timeouts or invalid moves"),
    ("$12.20", "for the whole batch"),
]
OVERHEADS = [
    ("161 s", "average 10,000-turn match"),
    ("198 s", "bot machines from zero to ready"),
    ("96%", "of machines on Spot"),
    ("1", "match replayed after a Spot reclaim"),
    ("10,000,000", "turns played"),
]
THEMES = {
    "light": {"panel": "#f6f8fa", "line": "#d0d7de", "ink": "#1f2328", "muted": "#59636e",
              "accent": ["#d9480f", "#1864ab"]},
    "dark": {"panel": "#151b23", "line": "#3d444d", "ink": "#f0f6fc", "muted": "#9198a1",
             "accent": ["#ff8a4c", "#5cb0ff"]},
}
W, ROW1, ROW2, PAD = 1000, 112, 76, 20
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


def svg(c):
    h = PAD + ROW1 + ROW2 + PAD
    col, col2 = (W - 2 * PAD) / len(HEADLINE), (W - 2 * PAD) / len(OVERHEADS)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" height="{h}" '
           f'font-family="{FONT}">',
           f'<rect x="0.5" y="0.5" width="{W - 1}" height="{h - 1}" rx="12" fill="{c["panel"]}" '
           f'stroke="{c["line"]}"/>',
           f'<line x1="{PAD}" y1="{PAD + ROW1}" x2="{W - PAD}" y2="{PAD + ROW1}" stroke="{c["line"]}"/>']
    for i, (number, caption) in enumerate(HEADLINE):
        x = PAD + col * (i + 0.5)
        out.append(f'<text x="{x}" y="{PAD + 58}" text-anchor="middle" font-size="44" font-weight="700" '
                   f'fill="{c["accent"][i % 2]}">{escape(number)}</text>')
        out.append(f'<text x="{x}" y="{PAD + 88}" text-anchor="middle" font-size="15" '
                   f'fill="{c["muted"]}">{escape(caption)}</text>')
    for i, (number, caption) in enumerate(OVERHEADS):
        x = PAD + col2 * (i + 0.5)
        out.append(f'<text x="{x}" y="{PAD + ROW1 + 34}" text-anchor="middle" font-size="24" '
                   f'font-weight="650" fill="{c["ink"]}">{escape(number)}</text>')
        out.append(f'<text x="{x}" y="{PAD + ROW1 + 58}" text-anchor="middle" font-size="12.5" '
                   f'fill="{c["muted"]}">{escape(caption)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


for theme, colours in THEMES.items():
    (Path(__file__).parent / f"stats-{theme}.svg").write_text(svg(colours))
