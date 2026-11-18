from __future__ import annotations

from dispatcher.breaker import CircuitBreaker


def test_it_trips_at_the_threshold_once_enough_attempts_finished() -> None:
    breaker = CircuitBreaker(min_attempts=20, threshold=0.05)
    for i in range(18):
        assert not breaker.record(float(i), controller_error=False)
    assert not breaker.record(18.0, controller_error=True), "19 attempts: below the minimum"
    assert breaker.record(19.0, controller_error=False), "1 error in 20 attempts is 5%"
    assert breaker.tripped
    assert not breaker.record(20.0, controller_error=True), "reports tripping only once"


def test_one_flaky_match_in_a_quiet_hour_doesnt_trip_it() -> None:
    breaker = CircuitBreaker(min_attempts=20)
    assert not breaker.record(0.0, controller_error=True)
    assert not breaker.tripped


def test_old_attempts_leave_the_window() -> None:
    breaker = CircuitBreaker(window_s=600, min_attempts=2, threshold=0.5)
    breaker.record(0.0, controller_error=True)
    breaker.record(700.0, controller_error=False)
    assert breaker.error_rate(700.0) == (0, 1)
    assert not breaker.tripped


def test_a_reset_forgets_the_old_errors() -> None:
    breaker = CircuitBreaker(min_attempts=2, threshold=0.5)
    breaker.record(0.0, controller_error=True)
    breaker.record(1.0, controller_error=True)
    assert breaker.tripped
    breaker.reset()
    assert not breaker.tripped
    assert not breaker.record(2.0, controller_error=True), "1 attempt after the reset"
