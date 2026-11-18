"""Sample bot for local matches: reads stdin, writes stdout, debug output to stderr.

This is all a contestant writes; the relay in front of it handles the network.
It plays the coin game (greedy) and Nim (optimal), detected from the init message.

BOT_MODE, for testing the platform:
  ok       plays normally
  slow     misses the time budget every 5th turn
  crash    exits on turn 3
  garbage  prints non-JSON and illegal moves
  huge     prints a line over 64 KiB on turn 2
  silent   never answers init
  noflush  never flushes stdout
  probe    tries every way out a bot must not have; plays garbage if any works
"""

import json
import os
import random
import socket
import sys
import time

MODE = os.environ.get("BOT_MODE", "ok")
MOVES = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}

# The relay sets PYTHONUNBUFFERED=1; noflush deliberately reopens stdout with a buffer.
out = os.fdopen(1, "w", buffering=1 << 16, closefd=False) if MODE == "noflush" else sys.stdout


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def answer(obj):
    out.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
    if MODE != "noflush":
        out.flush()


def coin_move(state, slot, config):
    """Walks toward the nearest coin."""
    x, y = state["positions"][slot]
    board, width, height = config["board"], config["width"], config["height"]
    options = [
        m
        for m, (dx, dy) in MOVES.items()
        if 0 <= x + dx < width and 0 <= y + dy < height and board[y + dy][x + dx] != "#"
    ]
    if not state["coins"] or not options:
        return "STAY"
    cx, cy = min(state["coins"], key=lambda c: abs(c[0] - x) + abs(c[1] - y))

    def dist(m):
        return abs(cx - (x + MOVES[m][0])) + abs(cy - (y + MOVES[m][1]))

    best = min(options, key=dist)
    return best if dist(best) < abs(cx - x) + abs(cy - y) else random.choice(options)


def nim_move(state):
    """Leaves the opponent a position whose heaps XOR to zero, if possible."""
    heaps = state["heaps"]
    total = 0
    for h in heaps:
        total ^= h
    for i, h in enumerate(heaps):
        if total and h ^ total < h:
            return {"heap": i, "take": h - (h ^ total)}
    first = next(i for i, h in enumerate(heaps) if h > 0)
    return {"heap": first, "take": 1}


def probe(slot):
    """Everything a bot must not reach. Returns what got through."""
    leaks = []
    targets = [
        ("1.1.1.1", 443, "the internet"),
        ("169.254.169.254", 80, "EC2 instance metadata"),
        ("169.254.170.2", 80, "ECS credentials endpoint"),
    ]
    for host, port, what in targets:
        try:
            socket.create_connection((host, port), timeout=1).close()
            leaks.append(f"{what} ({host}:{port})")
        except OSError:
            pass
    names = [
        "example.com",
        f"bot{1 - slot}",
    ]  # public DNS (a classic way to leak data), the other bot
    for name in names:
        try:
            socket.getaddrinfo(name, 443)
            leaks.append(f"DNS resolves {name}")
        except OSError:
            pass
    return leaks


def main():
    line = sys.stdin.readline()
    if not line:
        return
    init = json.loads(line)
    slot, config = init["slot"], init["config"]
    game = "coin" if "board" in config else "nim" if "heaps" in config else "unknown"
    log(f"slot {slot}, game {game}, mode {MODE}")

    leaks = probe(slot) if MODE == "probe" else []
    if MODE == "probe":
        log("PROBE: reachable: " + ", ".join(leaks) if leaks else "PROBE: nothing reachable")
    if MODE == "silent":
        for _ in sys.stdin:  # never answers; waits for the match to end
            pass
        return

    answer("ready")
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["type"] == "end":
            log(f"end: rank {msg['rank']}, score {msg['score']}")
            return
        if msg["type"] != "turn":
            continue
        turn, state = msg["turn"], msg["state"]
        if MODE == "slow" and turn % 5 == 0:
            time.sleep(msg["budget_ms"] / 1000 + 0.3)
        if MODE == "crash" and turn == 3:
            log("crashing on purpose")
            sys.exit(3)
        if MODE == "huge" and turn == 2:
            answer("x" * 70_000)
            continue
        if MODE == "garbage" or leaks:
            answer("not json" if turn % 2 else {"move": "TELEPORT"})
            continue
        if game == "coin":
            answer({"move": coin_move(state, slot, config)})
        elif game == "nim":
            answer(nim_move(state))
        else:
            answer({})  # a game this bot doesn't know: any well-formed answer keeps the match going


if __name__ == "__main__":
    main()
