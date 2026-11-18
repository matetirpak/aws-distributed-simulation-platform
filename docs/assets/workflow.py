"""The README's workflow diagram, as code (https://diagrams.mingrammer.com). Needs Graphviz.

  uv run --with diagrams --with pillow docs/assets/workflow.py

Writes docs/assets/workflow-light.png and workflow-dark.png, for GitHub's two themes. One row per
repository: the game, the platform code and the infrastructure, each from pull request to what it
delivers into the AWS account. Nodes are pinned (Graphviz's neato engine), as in architecture.py.
"""

import tempfile
from pathlib import Path

import diagrams
from diagrams import Diagram, Edge, Node
from diagrams.aws.compute import ECR, ECS
from diagrams.aws.network import VPC
from diagrams.custom import Custom
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.container import Docker
from diagrams.onprem.iac import Terraform
from diagrams.onprem.vcs import Github
from PIL import Image, ImageChops

HERE = Path(__file__).parent
ICONS = Path(diagrams.__file__).parent.parent
RECOLOURED = Path(tempfile.mkdtemp())
THEMES = {
    "light": {"text": "#1d2126", "muted": "#5f6770", "line": "#6e7781", "cluster": "#f3f2ee",
              "border": "#8a9099", "ink": None},
    "dark": {"text": "#e8eaed", "muted": "#a3abb4", "line": "#9aa3ad", "cluster": "#1b1f24",
             "border": "#7d858f", "ink": (232, 234, 237)},
}
ICON = 0.72  # half an icon, in inches: where an arrow meets an icon from above
GAME, PLATFORM, INFRA = 8.6, 4.6, 0.6  # the rows
PR, MAIN, BUILD, STORE, RUN = 0.0, 3.0, 6.0, 10.6, 13.6  # the columns


def at(x, y):
    return f"{x},{y}!"


def inked(cls, ink):
    """A copy of an icon with its black parts (the GitHub logo, the Terraform wordmark) in ink,
    so they stay visible on a dark background."""
    image = Image.open(ICONS / cls._icon_dir / cls._icon).convert("RGBA")
    r, g, b, alpha = image.split()
    black = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v < 90 else 0)
    rgb = Image.composite(Image.new("RGB", image.size, ink), image.convert("RGB"), black)
    image = Image.merge("RGBA", (*rgb.split(), alpha))
    path = RECOLOURED / f"{cls.__name__}.png"
    image.save(path)
    return str(path)


def draw(theme, c):
    graph = {"layout": "neato", "splines": "line", "outputorder": "nodesfirst", "bgcolor": "transparent",
             "pad": "0.4", "fontname": "Helvetica", "fontcolor": c["text"]}
    node = {"fontname": "Helvetica", "fontcolor": c["text"], "fontsize": "14"}

    def line(**attrs):
        return Edge(penwidth="1.3", arrowsize="0.8", color=c["line"], **attrs)

    def icon(cls, label, x, y):
        if c["ink"] and cls in (Github, Terraform):
            return Custom(label, inked(cls, c["ink"]), pos=at(x, y))
        return cls(label, pos=at(x, y))

    def pt(x, y):
        return Node("", shape="point", width="0.01", height="0.01", fixedsize="true", style="invis",
                    pos=at(x, y))

    def path(start, *points):
        """An arrow from start through each waypoint (x, y) to the last item, a node or a point."""
        *corners, end = points
        here = start
        for x, y in corners:
            nxt = pt(x, y)
            here - line() - nxt
            here = nxt
        if isinstance(end, tuple):
            end = pt(*end)
        here >> line() >> end

    def text(label, x, y, size="12", muted=True):
        Node(label, pos=at(x, y), shape="plaintext", fixedsize="false", width="0", height="0",
             margin="0", fontsize=size, fontcolor=c["muted"] if muted else c["text"])

    def box(label, left, bottom, right, top, fill=False):
        # neato doesn't draw clusters, so each repository and the account are box nodes, drawn first.
        Node(f"{label}\\l", pos=at((left + right) / 2, (bottom + top) / 2), shape="box", fixedsize="true",
             width=str(right - left), height=str(top - bottom), labelloc="t", margin="0.14,0.1",
             style="rounded" + (",filled" if fill else ""), fillcolor=c["cluster"], color=c["border"],
             fontcolor=c["text"], fontsize="14")

    with Diagram("", filename=str(HERE / f"workflow-{theme}"), outformat="png", show=False,
                 graph_attr=graph, node_attr=node):
        box("AWS account", 9.0, -1.45, 15.2, 10.1, fill=True)
        box("game repository", -1.3, 6.7, 7.6, 10.1)
        box("platform code: platform-game, here platform/", -1.3, 2.9, 7.6, 6.1)
        box("infrastructure: infra/ and .github/", -1.3, -1.45, 7.6, 2.1)

        # The game: checked on every PR; a version tag on main releases its controller image.
        game_pr = GithubActions("pull request\nfmt, clippy, tests", pos=at(PR, GAME))
        tag = icon(Github, "tag vX.Y.Z\non main", MAIN, GAME)
        release = GithubActions("release.yml\ntests, image build,\nsmoke match", pos=at(BUILD, GAME))
        controllers = ECR("game controller\nimages", pos=at(STORE, GAME))
        game_pr >> line() >> tag >> line() >> release >> line() >> controllers
        text("push role:\nv* tags only", 8.3, GAME + 0.42)
        text("Version tags are immutable. Each release records tag, commit and image digest.", 2.6, 7.02)

        # The platform: its images are built on top of a game's controller image.
        platform_pr = GithubActions("pull request\nRust, Python,\ncontract tests", pos=at(PR, PLATFORM))
        main = icon(Github, "main", MAIN, PLATFORM)
        build = Docker("push-image.sh\nmatch and bot images", pos=at(BUILD, PLATFORM))
        images = ECR("match and bot\nimages", pos=at(STORE, PLATFORM))
        matches = ECS("matches", pos=at(RUN, PLATFORM))
        platform_pr >> line() >> main >> line() >> build >> line() >> images >> line() >> matches
        text("tag: commit\nand game version", 8.3, PLATFORM + 0.42)
        text("run_batch.py", 12.1, PLATFORM + 0.3)
        path(pt(STORE, GAME - 1.25), (STORE, 6.5), (BUILD + 0.55, 6.5), (BUILD + 0.55, PLATFORM + ICON))
        text("controller image", 8.3, 6.72)

        # The infrastructure: planned on the PR, applied from main.
        infra_pr = GithubActions("pull request\nchecks, plan comment", pos=at(PR, INFRA))
        merge = icon(Github, "review, merge", MAIN, INFRA)
        plan = icon(Terraform, "deploy.yml\nsaved plan from main", BUILD, INFRA)
        account = VPC("network, cluster, bot fleet,\ndatabase, alarms", pos=at(12.1, INFRA))
        infra_pr >> line() >> merge >> line() >> plan >> line() >> account
        text("apply role:\nmain only", 8.3, INFRA + 0.42)
        text("plan role:\nread-only", PR, INFRA - 1.5)
        text("dev: applied in the same run if it destroys nothing.\n"
             "Otherwise an approver runs apply.yml.", BUILD - 1.1, INFRA - 1.5)


for theme, colours in THEMES.items():
    draw(theme, colours)
