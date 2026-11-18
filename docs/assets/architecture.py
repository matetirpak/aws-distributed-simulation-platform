"""The README's architecture diagram, as code (https://diagrams.mingrammer.com). Needs Graphviz.

  uv run --with diagrams --with pillow docs/assets/architecture.py

Writes docs/assets/architecture-light.png and architecture-dark.png, for GitHub's two themes.
Every node is pinned to a position (Graphviz's neato engine), and arrows bend through invisible
waypoints, so rows line up and every arrow runs straight. Parts that are designed but not built
yet have faded icons, a "planned" label and dashed arrows.
"""

import tempfile
from pathlib import Path

import diagrams
from diagrams import Diagram, Edge, Node
from diagrams.aws.compute import EC2, ECR, ECS, EC2AutoScaling, Fargate
from diagrams.aws.database import RDSPostgresqlInstance
from diagrams.aws.integration import SQS
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import Endpoint, Route53
from diagrams.aws.storage import S3
from diagrams.custom import Custom
from diagrams.programming.language import Python
from PIL import Image

HERE = Path(__file__).parent
ICONS = Path(diagrams.__file__).parent.parent
FADED = Path(tempfile.mkdtemp())
THEMES = {
    "light": {"text": "#1d2126", "muted": "#7a828b", "line": "#6e7781", "planned": "#a8aeb5",
              "cluster": "#f3f2ee", "border": "#8a9099", "fade": 0.35},
    "dark": {"text": "#e8eaed", "muted": "#8b939c", "line": "#9aa3ad", "planned": "#6b737c",
             "cluster": "#1b1f24", "border": "#7d858f", "fade": 0.4},
}
ICON = 0.72  # half an icon, in inches: where an arrow meets an icon from above


def at(x, y):
    return f"{x},{y}!"


def faded(cls, alpha):
    """A copy of an AWS icon, half grey and see-through, for the parts not built yet."""
    image = Image.open(ICONS / cls._icon_dir / cls._icon).convert("RGBA")
    grey = image.convert("LA").convert("RGBA")
    image = Image.blend(image, grey, 0.5)
    image.putalpha(image.getchannel("A").point(lambda a: round(a * alpha)))
    path = FADED / f"{cls.__name__}-{alpha}.png"
    image.save(path)
    return str(path)


def draw(theme, c):
    graph = {"layout": "neato", "splines": "line", "outputorder": "nodesfirst", "bgcolor": "transparent",
             "pad": "0.4", "fontname": "Helvetica", "fontcolor": c["text"]}
    node = {"fontname": "Helvetica", "fontcolor": c["text"], "fontsize": "14"}

    def line(planned=False, **attrs):
        # Edge() sets its own colour, which would override edge_attr: pass the theme's every time.
        style = {"color": c["planned"], "style": "dashed"} if planned else {"color": c["line"]}
        return Edge(penwidth="1.3", arrowsize="0.8", **{**style, **attrs})

    def pt(x, y):
        return Node("", shape="point", width="0.01", height="0.01", fixedsize="true", style="invis",
                    pos=at(x, y))

    def path(start, *points, planned=False):
        """An arrow from start through each waypoint (x, y) to the last item, a node or a point."""
        *corners, end = points
        here = start
        for x, y in corners:
            nxt = pt(x, y)
            here - line(planned) - nxt
            here = nxt
        if isinstance(end, tuple):
            end = pt(*end)
        here >> line(planned) >> end

    def text(label, x, y, planned=False, size="13"):
        Node(label, pos=at(x, y), shape="plaintext", fixedsize="false", width="0", height="0",
             margin="0", fontsize=size, fontcolor=c["muted"] if planned else c["text"])

    def planned(cls, label, x, y, tag=True):
        # Inside the planned builds box, the box says it once.
        label = f"{label}\n(planned)" if tag else label
        return Custom(label, faded(cls, c["fade"]), pos=at(x, y), fontcolor=c["muted"])

    def box(label, left, bottom, right, top, dashed=False, fill=False, planned=False):
        # neato doesn't draw clusters, so the VPC and the subnets are box nodes, drawn first.
        Node(f"{label}\\l", pos=at((left + right) / 2, (bottom + top) / 2), shape="box", fixedsize="true",
             width=str(right - left), height=str(top - bottom), labelloc="t", margin="0.14,0.1",
             style=("dashed," if dashed else "") + "rounded" + (",filled" if fill else ""),
             fillcolor=c["cluster"], color=c["border"], fontcolor=c["muted"] if planned else c["text"],
             fontsize="14")

    with Diagram("", filename=str(HERE / f"architecture-{theme}"), outformat="png", show=False,
                 graph_attr=graph, node_attr=node):
        box("VPC", 4.0, 2.2, 16.4, 9.3, fill=True)
        box("public subnets", 4.25, 3.85, 6.85, 6.75)
        box("private subnets: no internet route", 8.85, 2.45, 16.15, 8.5, dashed=True)
        box("one machine per bot, used once", 9.1, 2.7, 14.15, 8.05)

        # The batch: one match task per match.
        runner = Python("run_batch.py", pos=at(0, 5.3))
        ecs = ECS("ECS", pos=at(3.0, 5.3))
        match = Fargate("match task\ngame controller", pos=at(6.0, 5.3))
        runner >> line() >> ecs >> line() >> match
        text("RunTask,\none per match", 1.5, 5.62)

        # The match task asks for two bots. Each lands on a fresh machine and dials back.
        fleet = EC2AutoScaling("bot fleet\nSpot, on-demand fallback", pos=at(12.8, 5.3))
        bot_b = EC2("bot B", pos=at(10.0, 6.7))
        bot_a = EC2("bot A", pos=at(10.0, 3.9))
        path(match, (6.0, 5.3 + ICON), (6.0, 8.8), (12.8, 8.8), (12.8, 5.3 + ICON))
        text("RunTask ×2", 9.3, 9.0)
        for bot, y in ((bot_b, 6.7), (bot_a, 3.9)):
            path(fleet, (11.3, 5.3), (11.3, y), bot)
            path(bot, (8.6, y), (8.6, 5.3), match)
        text("TCP 9000\nsingle-use token", 7.73, 5.67)

        # Machines pull images and send logs through VPC endpoints; DNS will be filtered.
        endpoints = Endpoint("ECR, logs and\nS3 endpoints", pos=at(15.2, 5.3))
        images = ECR("controller and\nbot images", pos=at(18.6, 5.3))
        fleet >> line() >> endpoints >> line() >> images
        text("images, logs", 17.15, 5.55)
        dns = planned(Route53, "DNS Firewall", 15.2, 7.5)
        fleet >> line(planned=True) >> dns

        # What a match leaves behind, in one row under the match task.
        replays = S3("replays", pos=at(3.0, 0.4))
        results = RDSPostgresqlInstance("results", pos=at(6.0, 0.4))
        logs = Cloudwatch("logs and\ndashboard", pos=at(9.0, 0.4))
        trunk = pt(6.0, 1.75)
        pt(6.0, 4.1) - line() - trunk
        for target, x, label in ((replays, 3.0, "replay"), (results, 6.0, "result"), (logs, 9.0, "logs")):
            if x == 6.0:
                path(trunk, (x, 0.4 + ICON))
            else:
                path(trunk, (x, 1.75), (x, 0.4 + ICON))
            text(label, x + 0.12 + 0.045 * len(label), 1.42)

        # Planned: a dispatcher fed by a queue. A voided match goes back on the queue, up to three
        # attempts in all, and every result goes to the competition backend.
        box("match dispatch (planned)", -0.9, 12.95, 7.3, 15.6, dashed=True, planned=True)
        requests = planned(SQS, "match requests", 0, 14.4, tag=False)
        dispatcher = planned(Fargate, "dispatcher", 3.0, 14.4, tag=False)
        backend = planned(SQS, "results to\nthe backend", 6.0, 14.4, tag=False)
        path(pt(0.7, 14.68), (2.3, 14.68), planned=True)
        path(pt(2.3, 14.12), (0.7, 14.12), planned=True)
        text("voided: retry", 1.5, 13.93, planned=True, size="12")
        dispatcher >> line(planned=True) >> backend
        path(pt(3.0, 13.42), (3.0, 5.3 + ICON), planned=True)
        text("RunTask", 3.45, 9.6, planned=True)

        # Planned: submission builds. Untrusted source is compiled in a task with no role and no
        # internet, which only reaches its own objects through presigned URLs. The trusted builder
        # never runs anything from the build: it packages the one-file artifact into a bot image.
        box("submission builds (planned)", 8.2, 9.65, 20.0, 15.6, dashed=True, planned=True)
        box("build subnet\\lno internet route", 10.95, 9.85, 14.0, 12.7, planned=True)
        compile_queue = planned(SQS, "compile requests\nand results", 9.4, 14.4, tag=False)
        builder = planned(Fargate, "builder\npackages bot images", 12.8, 14.4, tag=False)
        toolchains = planned(ECR, "toolchain\nimages", 9.4, 11.2, tag=False)
        compiler = planned(Fargate, "compile task\none per build, no role", 12.8, 11.2, tag=False)
        bucket = planned(S3, "build bucket\nsources, artifacts, logs", 16.8, 11.2, tag=False)
        compile_queue - line(planned=True, forward=True, reverse=True) - builder
        path(builder, (12.8, 14.4 - 1.2), (12.8, 11.2 + ICON), planned=True)
        text("RunTask, presigned URLs", 14.08, 12.97, planned=True)
        toolchains >> line(planned=True) >> compiler
        compiler - line(planned=True, forward=True, reverse=True) - bucket
        text("source in,\nartifact and log out", 15.05, 11.62, planned=True)
        path(builder, (18.6, 14.4), (18.6, 5.3 + ICON), planned=True)
        text("bot images,\nby digest", 19.32, 8.0, planned=True)

        # Legend.
        path(pt(12.4, 0.9), (13.4, 0.9), planned=True)
        text("planned, not built yet", 14.55, 0.9, planned=True)


for theme, colours in THEMES.items():
    draw(theme, colours)
