"""The JSON the dispatcher exchanges: match requests from the backend, the match spec for the
controller, the controller's result, and the messages to the backend's results queue.

The match spec and the result mirror the Rust types in `crates/protocol` (`MatchSpec`,
`MatchResult`). The contract test (`tests/test_contract.py`) runs a spec built here through the
real controller and parses the result it writes, so the two sides can't drift apart unnoticed.

Tokens are secrets: they appear only inside the spec string handed to the controller task and in
the bot's start parameters, never in a log line, an exception message or a `repr`.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

#: Attempts per match: the first try plus two retries.
MAX_ATTEMPTS = 3

#: The controller's limit keys and defaults, as in `crates/protocol/src/spec.rs`.
DEFAULT_LIMITS: dict[str, int] = {
    "connect_ms": 300_000,
    "init_ms": 5_000,
    "turn_ms": 2_000,
    "grace_ms": 100,
    "max_turns": 500,
    "total_ms": 900_000,
    "max_timeouts": 3,
    "max_invalid": 10,
}

STATUSES = frozenset({"completed", "aborted", "controller_error"})
OUTCOMES = frozenset({"ok", "timeout", "disconnected", "invalid_output", "never_connected"})


class RequestError(ValueError):
    """A match request the dispatcher can't act on. Names the field, never echoes a value."""


class ResultError(ValueError):
    """A `result.json` that doesn't follow the contract."""


@dataclass(frozen=True)
class MatchRequest:
    """A match the backend wants played (`docs/contracts.md`, "Match requests").

    `attempt` is ours: the backend never sets it, and a retry is the same request with
    `attempt + 1`.
    """

    match_id: str
    submissions: tuple[str, str]
    ranked: bool = False
    game: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1

    @staticmethod
    def parse(body: str) -> MatchRequest:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RequestError("the request is not valid JSON") from None
        if not isinstance(data, dict):
            raise RequestError("the request must be a JSON object")
        known = {"match_id", "submissions", "ranked", "game", "attempt"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise RequestError(f"unknown field `{unknown[0]}`")

        match_id = data.get("match_id")
        if not isinstance(match_id, str) or not 0 < len(match_id) <= 128:
            raise RequestError("`match_id` must be a non-empty string of at most 128 characters")
        submissions = data.get("submissions")
        if (
            not isinstance(submissions, list)
            or len(submissions) != 2
            or not all(isinstance(s, str) and s for s in submissions)
        ):
            raise RequestError("`submissions` must be two non-empty strings, in slot order")
        ranked = data.get("ranked", False)
        if not isinstance(ranked, bool):
            raise RequestError("`ranked` must be true or false")
        game = data.get("game", {})
        if not isinstance(game, dict):
            raise RequestError("`game` must be an object")
        attempt = data.get("attempt", 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise RequestError("`attempt` must be an integer of at least 1")
        return MatchRequest(match_id, (submissions[0], submissions[1]), ranked, game, attempt)

    def to_json(self) -> str:
        return json.dumps(
            {
                "match_id": self.match_id,
                "submissions": list(self.submissions),
                "ranked": self.ranked,
                "game": self.game,
                "attempt": self.attempt,
            },
            separators=(",", ":"),
        )

    def next_attempt(self) -> MatchRequest:
        return dataclasses.replace(self, attempt=self.attempt + 1)


@dataclass(frozen=True)
class Secrets:
    """The two single-use bot tokens and the seed of one attempt. `repr` hides the tokens."""

    tokens: tuple[str, str]
    seed: int

    @staticmethod
    def new() -> Secrets:
        # 128-bit tokens; a 32-bit seed, so JavaScript consumers don't lose precision.
        return Secrets((secrets.token_hex(16), secrets.token_hex(16)), secrets.randbits(32))

    def __repr__(self) -> str:
        return f"Secrets(tokens=<redacted>, seed={self.seed})"


def build_spec(request: MatchRequest, secret: Secrets, limits: dict[str, int] | None = None) -> str:
    """The `MATCH_SPEC` for the controller. Only the keys the controller knows; it rejects others.

    `limits` overrides the controller's defaults key by key; the controller merges the rest.
    """
    limits = limits or {}
    unknown = sorted(set(limits) - set(DEFAULT_LIMITS))
    if unknown:
        raise ValueError(f"unknown limit `{unknown[0]}`")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in limits.values()):
        raise ValueError("limits must be positive integers")
    spec: dict[str, Any] = {
        "match_id": request.match_id,
        "attempt": request.attempt,
        "seed": secret.seed,
        "players": [{"slot": slot, "token": token} for slot, token in enumerate(secret.tokens)],
        "game": request.game,
    }
    if limits:
        spec["limits"] = dict(limits)
    return json.dumps(spec, separators=(",", ":"))


def effective_limits(limits: dict[str, int] | None) -> dict[str, int]:
    """The limits the controller will use: the defaults, overridden key by key."""
    return {**DEFAULT_LIMITS, **(limits or {})}


@dataclass(frozen=True)
class PlayerResult:
    slot: int
    rank: int | None
    score: int | None
    outcome: str
    timeouts: int
    invalid_actions: int


@dataclass(frozen=True)
class MatchResult:
    """The controller's `result.json`. `raw` is the parsed document, forwarded to the backend."""

    match_id: str
    attempt: int
    status: str
    turns: int
    players: tuple[PlayerResult, ...]
    raw: dict[str, Any] = field(repr=False, compare=False)


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultError(f"`{name}` must be an integer")
    return value


def _opt_int(value: Any, name: str) -> int | None:
    return None if value is None else _int(value, name)


def parse_result(text: str) -> MatchResult:
    """Parses and checks a `result.json` written by the controller."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ResultError("result.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise ResultError("result.json must be a JSON object")
    match_id = data.get("match_id")
    if not isinstance(match_id, str) or not match_id:
        raise ResultError("`match_id` must be a non-empty string")
    status = data.get("status")
    if status not in STATUSES:
        raise ResultError("`status` is not a known status")
    raw_players = data.get("players")
    if not isinstance(raw_players, list) or not raw_players:
        raise ResultError("`players` must be a non-empty list")
    players = []
    for i, p in enumerate(raw_players):
        if not isinstance(p, dict):
            raise ResultError(f"`players[{i}]` must be an object")
        if p.get("outcome") not in OUTCOMES:
            raise ResultError(f"`players[{i}].outcome` is not a known outcome")
        players.append(
            PlayerResult(
                slot=_int(p.get("slot"), f"players[{i}].slot"),
                rank=_opt_int(p.get("rank"), f"players[{i}].rank"),
                score=_opt_int(p.get("score"), f"players[{i}].score"),
                outcome=p["outcome"],
                timeouts=_int(p.get("timeouts"), f"players[{i}].timeouts"),
                invalid_actions=_int(p.get("invalid_actions"), f"players[{i}].invalid_actions"),
            )
        )
    if sorted(p.slot for p in players) != list(range(len(players))):
        raise ResultError("`players` must have the slots 0..n-1")
    return MatchResult(
        match_id=match_id,
        attempt=_int(data.get("attempt"), "attempt"),
        status=status,
        turns=_int(data.get("turns"), "turns"),
        players=tuple(sorted(players, key=lambda p: p.slot)),
        raw=data,
    )


# Messages to the backend's results queue: the draft in docs/contracts.md
# ("Results to the backend"); the final format is still to settle with the competition's backend.


def completed_message(request: MatchRequest, result: MatchResult, replay: str) -> dict[str, Any]:
    return {
        "match_id": request.match_id,
        "attempt": request.attempt,
        "status": "completed",
        "result": result.raw,
        "replay": replay,
    }


def voided_message(request: MatchRequest, reason: str, *, retry: bool) -> dict[str, Any]:
    return {
        "match_id": request.match_id,
        "attempt": request.attempt,
        "status": "voided",
        "reason": reason,
        # Whether the platform will play this match again.
        "retry": retry,
    }
