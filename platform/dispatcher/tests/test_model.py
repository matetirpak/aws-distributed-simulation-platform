from __future__ import annotations

import json

import pytest

from dispatcher.model import (
    DEFAULT_LIMITS,
    MatchRequest,
    RequestError,
    ResultError,
    Secrets,
    build_spec,
    parse_result,
)

from .fakes import request, result_json


def test_a_request_parses_with_defaults() -> None:
    r = MatchRequest.parse('{"match_id": "m-1", "submissions": ["a", "b"]}')
    assert (r.match_id, r.submissions, r.ranked, r.game, r.attempt) == (
        "m-1",
        ("a", "b"),
        False,
        {},
        1,
    )
    assert MatchRequest.parse(r.next_attempt().to_json()).attempt == 2


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"match_id": ""}, "match_id"),
        ({"match_id": "x" * 129}, "match_id"),
        ({"submissions": ["a"]}, "submissions"),
        ({"submissions": ["a", ""]}, "submissions"),
        ({"ranked": "yes"}, "ranked"),
        ({"game": []}, "game"),
        ({"attempt": 0}, "attempt"),
        ({"attempt": True}, "attempt"),
        ({"extra": 1}, "extra"),
    ],
)
def test_a_bad_request_names_the_field_without_echoing_it(
    change: dict[str, object], field: str
) -> None:
    body = {**json.loads(request()), **change}
    with pytest.raises(RequestError) as error:
        MatchRequest.parse(json.dumps(body))
    assert field in str(error.value)
    assert "x" * 20 not in str(error.value)


def test_the_spec_has_exactly_the_keys_the_controller_knows() -> None:
    r = MatchRequest.parse(request())
    secret = Secrets.new()
    spec = json.loads(build_spec(r, secret))
    assert set(spec) == {"match_id", "attempt", "seed", "players", "game"}
    assert spec["players"] == [
        {"slot": 0, "token": secret.tokens[0]},
        {"slot": 1, "token": secret.tokens[1]},
    ]
    with_limits = json.loads(build_spec(r, secret, {"connect_ms": 500}))
    assert with_limits["limits"] == {"connect_ms": 500}
    with pytest.raises(ValueError, match="unknown limit"):
        build_spec(r, secret, {"speed": 1})
    with pytest.raises(ValueError, match="positive"):
        build_spec(r, secret, {"turn_ms": 0})
    assert set(DEFAULT_LIMITS) == {
        "connect_ms",
        "init_ms",
        "turn_ms",
        "grace_ms",
        "max_turns",
        "total_ms",
        "max_timeouts",
        "max_invalid",
    }


def test_secrets_are_fresh_and_hidden() -> None:
    a, b = Secrets.new(), Secrets.new()
    assert all(len(t) == 32 and int(t, 16) >= 0 for t in a.tokens)
    assert len({*a.tokens, *b.tokens}) == 4
    assert 0 <= a.seed < 2**32
    assert not any(t in repr(a) for t in a.tokens)


def test_a_result_parses() -> None:
    result = parse_result(result_json("m-1", 1, outcomes=("ok", "timeout")))
    assert (result.match_id, result.attempt, result.status, result.turns) == (
        "m-1",
        1,
        "completed",
        42,
    )
    assert [p.outcome for p in result.players] == ["ok", "timeout"]
    assert result.raw["controller_version"] == "0.1.0"


@pytest.mark.parametrize(
    "change",
    [
        {"status": "won"},
        {"players": []},
        {"turns": "42"},
        {"attempt": None},
    ],
)
def test_a_bad_result_is_rejected(change: dict[str, object]) -> None:
    body = {**json.loads(result_json("m-1", 1)), **change}
    with pytest.raises(ResultError):
        parse_result(json.dumps(body))


def test_a_result_needs_slots_0_to_n() -> None:
    body = json.loads(result_json("m-1", 1))
    body["players"][1]["slot"] = 5
    with pytest.raises(ResultError, match="slots"):
        parse_result(json.dumps(body))
