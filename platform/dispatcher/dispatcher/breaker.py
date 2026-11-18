"""The circuit breaker on `controller_error` (docs/architecture.md).

A broken controller release would void almost every match, and each void is retried on the same
broken image. The breaker stops that: when `controller_error` reaches 5% of the attempts finished
in the last 10 minutes (with at least 20 attempts in that window, so one flaky match in a quiet
hour doesn't count), it trips. A tripped dispatcher takes no new matches; running ones finish.
Only an operator resets it, after rolling back.

Each dispatcher task counts its own attempts. A broken release trips both within minutes anyway.
"""

from __future__ import annotations

from collections import deque


class CircuitBreaker:
    def __init__(
        self, *, window_s: float = 600.0, threshold: float = 0.05, min_attempts: int = 20
    ) -> None:
        self.window_s = window_s
        self.threshold = threshold
        self.min_attempts = min_attempts
        self._finished: deque[tuple[float, bool]] = deque()  # (time, was a controller error)
        self.tripped = False

    def record(self, now: float, *, controller_error: bool) -> bool:
        """Records one finished attempt. Returns True if this record tripped the breaker."""
        self._finished.append((now, controller_error))
        self._forget(now)
        if self.tripped:
            return False
        total = len(self._finished)
        errors = sum(1 for _, error in self._finished if error)
        if total >= self.min_attempts and errors / total >= self.threshold:
            self.tripped = True
            return True
        return False

    def reset(self) -> None:
        """An operator's reset: forget the window, so the old errors can't trip it again."""
        self._finished.clear()
        self.tripped = False

    def error_rate(self, now: float) -> tuple[int, int]:
        """(controller errors, finished attempts) in the window."""
        self._forget(now)
        return sum(1 for _, e in self._finished if e), len(self._finished)

    def _forget(self, now: float) -> None:
        while self._finished and self._finished[0][0] <= now - self.window_s:
            self._finished.popleft()
