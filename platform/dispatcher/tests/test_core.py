"""The dispatcher's decisions, scenario by scenario, against the fakes in `fakes.py`."""

from __future__ import annotations

import json
import logging

import pytest

from dispatcher.breaker import CircuitBreaker
from dispatcher.core import Settings
from dispatcher.log import JsonFormatter
from dispatcher.ports import Role

from .fakes import World, request, result_json, world

KEY = ("m-1", 1)


def start_match(w: World, key: tuple[str, int] = KEY) -> None:
    """Receive the request, the controller comes up, both bots come up."""
    w.dispatcher.tick()
    w.tasks.running(key, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    w.tasks.running(key, Role.BOT0)
    w.tasks.running(key, Role.BOT1)
    w.dispatcher.tick()


def finish_controller(w: World, key: tuple[str, int], code: int, status: str | None) -> None:
    if status is not None:
        w.store.results[key] = result_json(key[0], key[1], status)
    w.tasks.exited(key, Role.CONTROLLER, code)
    w.dispatcher.tick()


def test_a_match_is_played_and_reported() -> None:
    w = world()
    w.queue.put(request())

    w.dispatcher.tick()
    assert KEY in w.tasks.specs, "the controller is started first"
    assert not w.tasks.bot_starts, "bots wait for the controller's address"

    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    starts = {b.slot: b for b in w.tasks.bot_starts}
    assert [starts[0].submission, starts[1].submission] == ["sub-a", "sub-b"]
    assert {b.address for b in starts.values()} == {"10.0.1.5:9000"}
    assert [starts[0].token, starts[1].token] == w.tasks.tokens_in_spec(KEY)
    assert not any(b.on_demand for b in starts.values()), "ladder bots start on Spot"

    w.tasks.running(KEY, Role.BOT0)
    w.tasks.running(KEY, Role.BOT1)
    w.dispatcher.tick()
    finish_controller(w, KEY, 0, "completed")

    [message] = w.results.sent
    assert message["status"] == "completed"
    assert message["result"]["players"][0]["outcome"] == "ok"
    assert message["replay"] == "mem://replays/m-1/1/replay.json.gz"
    assert w.store.statuses[KEY]["status"] == "completed"
    assert w.tasks.stopped == [KEY], "leftover tasks are cleaned up"
    assert not w.queue.sent, "nothing to retry"
    assert not w.queue.entries, "the request is deleted"
    assert not w.dispatcher.busy


def test_the_spec_carries_the_request_and_the_limits() -> None:
    w = world(Settings(limits={"connect_ms": 30_000}))
    w.queue.put(request())
    w.dispatcher.tick()
    spec = json.loads(w.tasks.specs[KEY])
    assert spec["match_id"] == "m-1"
    assert spec["attempt"] == 1
    assert spec["game"] == {"map": "arena-3"}
    assert spec["limits"] == {"connect_ms": 30_000}
    assert [p["slot"] for p in spec["players"]] == [0, 1]


def test_a_bot_that_never_starts_voids_the_attempt_and_retries() -> None:
    w = world()
    w.queue.put(request())
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.BOT0)  # bot 1 never gets a machine
    w.clock.advance(300)
    w.dispatcher.tick()

    [message] = w.results.sent
    assert (message["status"], message["reason"], message["retry"]) == (
        "voided",
        "bot_never_started",
        True,
    )
    [(body, delay)] = w.queue.sent
    assert json.loads(body)["attempt"] == 2
    assert delay == 10
    assert w.tasks.stopped == [KEY]


def test_the_third_void_is_final(caplog: pytest.LogCaptureFixture) -> None:
    w = world()
    w.queue.put(request(attempt=3))
    key = ("m-1", 3)
    w.dispatcher.tick()
    w.tasks.running(key, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    w.clock.advance(300)
    with caplog.at_level(logging.ERROR):
        w.dispatcher.tick()
    [message] = w.results.sent
    assert message["retry"] is False
    assert not w.queue.sent
    assert any("giving up" in r.getMessage() for r in caplog.records)


def test_a_spot_bot_that_doesnt_start_is_relaunched_on_demand() -> None:
    w = world()
    w.queue.put(request())
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.BOT0)
    w.clock.advance(60)
    w.dispatcher.tick()
    relaunch = w.tasks.bot_starts[-1]
    assert (relaunch.slot, relaunch.on_demand) == (1, True)
    w.clock.advance(5)
    w.dispatcher.tick()
    assert len(w.tasks.bot_starts) == 3, "relaunched only once"


def test_finals_start_on_demand_without_a_fallback() -> None:
    w = world()
    w.queue.put(request(ranked=True))
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    assert all(b.on_demand for b in w.tasks.bot_starts)
    w.clock.advance(120)
    w.dispatcher.tick()
    assert len(w.tasks.bot_starts) == 2


def test_a_reclaimed_bot_voids_the_match_even_mid_game() -> None:
    w = world()
    w.queue.put(request())
    start_match(w)
    w.tasks.reclaimed(KEY, Role.BOT1)
    w.dispatcher.tick()
    [message] = w.results.sent
    assert (message["status"], message["reason"]) == ("voided", "infrastructure")
    assert message["retry"] is True
    assert w.tasks.stopped == [KEY], "the rest of the match is stopped right away"


def test_a_bot_that_crashes_on_its_own_loses_but_the_match_counts() -> None:
    w = world()
    w.queue.put(request())
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.BOT0)
    w.tasks.running(KEY, Role.BOT1)
    w.tasks.exited(KEY, Role.BOT1, 1)  # the contestant's process exited by itself
    w.dispatcher.tick()
    w.store.results[KEY] = result_json("m-1", 1, outcomes=("ok", "disconnected"))
    w.tasks.exited(KEY, Role.CONTROLLER, 0)
    w.dispatcher.tick()
    [message] = w.results.sent
    assert message["status"] == "completed"
    assert message["result"]["players"][1]["outcome"] == "disconnected"


@pytest.mark.parametrize(
    ("code", "status", "reason", "retry", "counts_as_error"),
    [
        (1, "controller_error", "controller_error", True, True),
        (1, None, "controller_error", True, True),  # crashed without writing a result
        (1, "aborted", "aborted", True, False),
        (2, None, "bad_config", False, False),
    ],
)
def test_how_a_failed_controller_is_decided(
    code: int, status: str | None, reason: str, retry: bool, counts_as_error: bool
) -> None:
    w = world()
    w.queue.put(request())
    start_match(w)
    finish_controller(w, KEY, code, status)
    [message] = w.results.sent
    assert (message["status"], message["reason"], message["retry"]) == ("voided", reason, retry)
    assert w.dispatcher.breaker.error_rate(w.clock.now)[0] == int(counts_as_error)
    assert bool(w.queue.sent) == retry


def test_a_result_of_another_attempt_is_not_trusted() -> None:
    w = world()
    w.queue.put(request())
    start_match(w)
    w.store.results[KEY] = result_json("m-1", 2)
    w.tasks.exited(KEY, Role.CONTROLLER, 0)
    w.dispatcher.tick()
    assert w.results.sent[0]["reason"] == "controller_error"


def test_a_stuck_controller_is_stopped_and_voided() -> None:
    w = world(Settings(limits={"connect_ms": 1_000, "total_ms": 10_000}))
    w.queue.put(request())
    start_match(w)
    w.clock.advance(1 + 10 + 120)
    w.dispatcher.tick()
    [message] = w.results.sent
    assert message["reason"] == "stuck"
    assert w.tasks.stopped == [KEY]


def test_messages_of_running_matches_stay_invisible() -> None:
    w = world()
    w.queue.put(request())
    start_match(w)
    w.clock.advance(61)
    w.dispatcher.tick()
    assert w.queue.extended, "extended before half the visibility timeout is gone"
    _receipt, seconds = w.queue.extended[-1]
    assert seconds == 120
    assert w.queue.receive(10) == [], "the other dispatcher doesn't see it"


def test_a_crashed_dispatchers_match_is_adopted() -> None:
    w = world()
    # What the crashed dispatcher left behind: all three tasks, playing.
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.tasks.running(KEY, Role.BOT0)
    w.tasks.running(KEY, Role.BOT1)
    w.queue.put(request())
    w.dispatcher.tick()
    assert KEY not in w.tasks.specs, "no second controller"
    assert not w.tasks.bot_starts, "no new bots: the controller has the old tokens"
    finish_controller(w, KEY, 0, "completed")
    assert w.results.sent[0]["status"] == "completed"


def test_a_half_started_attempt_is_voided() -> None:
    w = world()
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")  # bots were never started
    w.queue.put(request())
    w.dispatcher.tick()
    [message] = w.results.sent
    assert (message["reason"], message["retry"]) == ("dispatcher_restarted", True)


def test_an_attempt_already_reported_is_not_played_again() -> None:
    w = world()
    w.store.statuses[KEY] = {"status": "completed"}
    w.queue.put(request())
    w.dispatcher.tick()
    assert not w.tasks.specs
    assert not w.queue.entries, "the late copy is deleted"
    assert not w.results.sent


def test_a_bad_request_is_left_for_the_dead_letter_queue(caplog: pytest.LogCaptureFixture) -> None:
    w = world()
    w.queue.put('{"match_id": "m-1"}')
    with caplog.at_level(logging.ERROR):
        w.dispatcher.tick()
    assert not w.tasks.specs
    assert len(w.queue.entries) == 1, "not deleted"
    assert any(r.getMessage() == "bad match request" for r in caplog.records)


def test_capacity_limits_concurrent_matches() -> None:
    w = world(Settings(capacity=1))
    w.queue.put(request("m-1"))
    w.queue.put(request("m-2"))
    w.dispatcher.tick()
    assert list(w.tasks.specs) == [KEY]
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    w.tasks.running(KEY, Role.BOT0)
    w.tasks.running(KEY, Role.BOT1)
    finish_controller(w, KEY, 0, "completed")
    assert ("m-2", 1) not in w.tasks.specs, "at capacity, m-2 was never received"
    w.dispatcher.tick()  # m-1 is done, so there's room again
    assert ("m-2", 1) in w.tasks.specs


def test_a_tripped_breaker_stops_intake_until_an_operator_resets_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    w = world(breaker=CircuitBreaker(min_attempts=2, threshold=0.5))
    for i in (1, 2):
        w.queue.put(request(f"m-{i}"))
    w.dispatcher.tick()
    for i in (1, 2):
        key = (f"m-{i}", 1)
        w.tasks.running(key, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    for i in (1, 2):
        key = (f"m-{i}", 1)
        w.tasks.running(key, Role.BOT0)
        w.tasks.running(key, Role.BOT1)
    w.dispatcher.tick()
    with caplog.at_level(logging.ERROR):
        for i in (1, 2):
            key = (f"m-{i}", 1)
            w.store.results[key] = result_json(key[0], 1, "controller_error")
            w.tasks.exited(key, Role.CONTROLLER, 1)
        w.dispatcher.tick()
    assert w.dispatcher.breaker.tripped
    assert any("circuit breaker tripped" in r.getMessage() for r in caplog.records)

    w.clock.advance(11)  # the retries are visible now, but nobody takes them
    w.dispatcher.tick()
    assert ("m-1", 2) not in w.tasks.specs

    w.control.reset = True
    w.dispatcher.tick()
    assert not w.dispatcher.breaker.tripped
    assert {("m-1", 2), ("m-2", 2)} <= set(w.tasks.specs)


def test_no_token_ever_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    w = world()
    w.queue.put(request())
    with caplog.at_level(logging.DEBUG):
        start_match(w)
        w.tasks.reclaimed(KEY, Role.BOT0)
        w.dispatcher.tick()
    formatter = JsonFormatter()
    text = "\n".join(formatter.format(r) for r in caplog.records)
    tokens = w.tasks.tokens_in_spec(KEY)
    assert text, "the scenario logs something"
    assert not any(token in text for token in tokens)
    assert not any(token in repr(a) for a in w.dispatcher.attempts.values() for token in tokens)


def test_a_bot_stopped_by_an_operator_voids_the_match() -> None:
    w = world()
    w.queue.put(request())
    start_match(w)
    w.tasks.stop_one(KEY, Role.BOT0)  # e.g. the reaper, or someone running StopTask
    w.dispatcher.tick()
    [message] = w.results.sent
    assert (message["status"], message["reason"], message["retry"]) == ("voided", "stopped", True)


def test_a_failed_controller_start_is_submitted_again(caplog: pytest.LogCaptureFixture) -> None:
    w = world()
    w.tasks.fail_next_controller_start = True
    w.queue.put(request())
    with caplog.at_level(logging.ERROR):
        w.dispatcher.tick()
    assert any("trying again next tick" in r.getMessage() for r in caplog.records)
    assert KEY in w.tasks.specs, "the watch step of the same tick submitted it again"
    assert not w.results.sent, "a transient error voids nothing"
    w.tasks.running(KEY, Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    assert len(w.tasks.bot_starts) == 2


def test_one_failing_match_doesnt_stop_the_others() -> None:
    w = world()
    w.tasks.fail_bot_starts_for = "m-1"
    for i in (1, 2):
        w.queue.put(request(f"m-{i}"))
    w.dispatcher.tick()
    for i in (1, 2):
        w.tasks.running((f"m-{i}", 1), Role.CONTROLLER, address="10.0.1.5:9000")
    w.dispatcher.tick()
    assert {b.key for b in w.tasks.bot_starts} == {("m-2", 1)}, "m-2 goes on"
    w.tasks.fail_bot_starts_for = None
    w.dispatcher.tick()
    assert {b.key for b in w.tasks.bot_starts} == {("m-1", 1), ("m-2", 1)}, "m-1 tries again"
