"""Plays matches locally through the real dispatcher logic:

    uv run python -m dispatcher local --game coin --match ok,ok --match ok,absent --match ok,crash

Each `--match` is one match request; its two submissions are bot modes (`bots/python/bot.py`),
or `absent` for a bot that never starts. Runs until every match has a final result, then prints
what went to the results "queue" (`<out>/results.jsonl`).
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import time
from pathlib import Path

from . import log
from .core import Dispatcher, Settings
from .local import REPO, ComposeTasks, DirStore, FileControl, FileResults, LocalQueue, SystemClock
from .model import MatchRequest

logger = logging.getLogger("dispatcher")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dispatcher")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local", help="play matches locally with docker compose")
    local.add_argument("--game", default="coin", help="the mock controller's game (MOCK_GAME)")
    local.add_argument(
        "--match",
        action="append",
        required=True,
        metavar="MODE0,MODE1",
        help="one match: the two bots' modes (repeatable)",
    )
    local.add_argument(
        "--controller-image", help="a real game's controller image instead of the mock"
    )
    local.add_argument("--out", type=Path, default=REPO / "out" / "dispatcher")
    local.add_argument("--bot-start-deadline", type=float, default=20.0, metavar="SECONDS")
    local.add_argument("--poll", type=float, default=1.0, metavar="SECONDS")
    args = parser.parse_args(argv)

    log.setup()
    pairs = [tuple(m.split(",")) for m in args.match]
    if any(len(p) != 2 or not all(p) for p in pairs):
        parser.error("--match takes two modes, e.g. ok,absent")

    out: Path = args.out.resolve()
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    queue = LocalQueue()
    for i, (mode0, mode1) in enumerate(pairs):
        request = MatchRequest(f"local-{run_id}-{i}", (mode0, mode1))
        queue.put(request.to_json())

    tasks = ComposeTasks(out, mock_game=args.game, controller_image=args.controller_image)
    logger.info("building the images (once)")
    tasks.build()
    results_path = out / "results.jsonl"
    start_line = _count_lines(results_path)
    dispatcher = Dispatcher(
        queue=queue,
        tasks=tasks,
        store=DirStore(out),
        results=FileResults(results_path),
        control=FileControl(out / "reset-breaker"),
        clock=SystemClock(),
        settings=Settings(
            bot_start_deadline_s=args.bot_start_deadline,
            spot_fallback_s=float("inf"),  # no Spot locally
            retry_delay_s=1,
            # Locally the bots start in seconds; AWS keeps the 5-minute default.
            limits={"connect_ms": 30_000},
        ),
    )
    while not queue.empty() or dispatcher.busy:
        dispatcher.tick()
        time.sleep(args.poll)

    print(f"\nresults ({results_path}):")
    for line in results_path.read_text().splitlines()[start_line:]:
        message = json.loads(line)
        detail = message.get("reason") or ", ".join(
            f"slot {p['slot']}: {p['outcome']}" for p in message["result"]["players"]
        )
        retry = " (retrying)" if message.get("retry") else ""
        where = f"{message['match_id']} attempt {message['attempt']}"
        print(f"  {where}: {message['status']}, {detail}{retry}")
    return 0


def _count_lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


if __name__ == "__main__":
    sys.exit(main())
