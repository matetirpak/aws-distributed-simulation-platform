"""The dispatcher's logic: turns match requests into running matches and reports the results.

One `tick()`, every few seconds:

1. Receive new match requests, unless the circuit breaker tripped or we're at capacity.
2. Start each new attempt: the controller first, then both bots once it has an address. If the
   attempt's tasks already exist (a dispatcher crashed and its message came back), adopt them.
3. Check every running attempt's tasks and move it on:
   - a bot that never runs within the deadline voids the attempt (`bot_never_started`)
   - any task stopped by the infrastructure (Spot, host failure) voids it (`infrastructure`)
   - a controller running far past its time limits is stopped (`stuck`)
   - when the controller stops, its exit code and `result.json` decide (`_finish`)
4. Keep the queue messages of running attempts invisible to the other dispatcher (heartbeat).

Reporting an attempt always goes: results message, `status.json`, stop leftover tasks, queue
the retry (if any), delete the original message. Deleting last means a crash in between leaves
the message to be received again, and `status.json` stops it from being played twice.

Infrastructure failures never count as a loss: they void the attempt, which is retried with
`attempt + 1`, at most `MAX_ATTEMPTS` times in total.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from .breaker import CircuitBreaker
from .log import fields
from .model import (
    MAX_ATTEMPTS,
    MatchRequest,
    MatchResult,
    RequestError,
    ResultError,
    Secrets,
    build_spec,
    completed_message,
    effective_limits,
    parse_result,
    voided_message,
)
from .ports import (
    Clock,
    Control,
    Message,
    Queue,
    Results,
    Role,
    Status,
    StopReason,
    Store,
    Tasks,
    TaskState,
)

log = logging.getLogger("dispatcher")

Key = tuple[str, int]


@dataclass(frozen=True)
class Settings:
    #: Matches one dispatcher task runs at once.
    capacity: int = 50
    #: The queue's visibility timeout. A message is extended when half of it is left.
    visibility_s: int = 120
    #: How long the controller, and then both bots, may take to start. ECS itself has no deadline.
    bot_start_deadline_s: float = 300.0
    #: A ladder bot still not running on Spot after this long is relaunched on-demand.
    spot_fallback_s: float = 60.0
    #: Added to `connect_ms + total_ms` before a running controller counts as stuck.
    stuck_margin_s: float = 120.0
    #: Delay before a retried attempt becomes visible.
    retry_delay_s: int = 10
    #: Limit overrides for every match spec (e.g. a short `connect_ms` locally).
    limits: dict[str, int] = field(default_factory=dict)


class Phase(Enum):
    STARTING_CONTROLLER = auto()
    STARTING_BOTS = auto()
    RUNNING = auto()


@dataclass
class Attempt:
    request: MatchRequest
    message: Message
    secret: Secrets = field(repr=False)
    received_at: float
    visible_until: float
    phase: Phase = Phase.STARTING_CONTROLLER
    bots_started_at: float = 0.0
    #: The controller's start was submitted (a failed submission is retried on the next tick).
    controller_requested: bool = False
    #: Per slot: started on on-demand capacity already (finals always, ladder after the fallback).
    on_demand: list[bool] = field(default_factory=lambda: [False, False])

    @property
    def key(self) -> Key:
        return (self.request.match_id, self.request.attempt)

    def ids(self) -> dict[str, Any]:
        return {"match_id": self.request.match_id, "attempt": self.request.attempt}


def _started(state: TaskState | None) -> bool:
    """Running, or ran and exited on its own (a contestant's crash is their loss, not a void)."""
    return state is not None and (
        state.status is Status.RUNNING
        or (state.status is Status.STOPPED and state.stop_reason is StopReason.EXITED)
    )


class Dispatcher:
    def __init__(
        self,
        *,
        queue: Queue,
        tasks: Tasks,
        store: Store,
        results: Results,
        control: Control,
        clock: Clock,
        settings: Settings | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.queue = queue
        self.tasks = tasks
        self.store = store
        self.results = results
        self.control = control
        self.clock = clock
        self.settings = settings or Settings()
        self.breaker = breaker or CircuitBreaker()
        self.attempts: dict[Key, Attempt] = {}

    @property
    def busy(self) -> bool:
        return bool(self.attempts)

    def tick(self) -> None:
        now = self.clock.monotonic()
        with self._guard("reading the operator controls"):
            if self.control.breaker_reset_requested() and self.breaker.tripped:
                self.breaker.reset()
                log.warning("circuit breaker reset by an operator; taking matches again")
        if not self.breaker.tripped:
            with self._guard("receiving match requests"):
                self._receive(now)
        with self._guard("checking the tasks"):
            self._advance(now)
        self._heartbeat(now)

    @contextmanager
    def _guard(self, doing: str, attempt: Attempt | None = None) -> Iterator[None]:
        """One failing call (a docker hiccup, an AWS throttle) must not stop the other matches:
        log it, and the next tick tries again. Every step is idempotent, so retrying is safe."""
        try:
            yield
        except Exception:
            ids = attempt.ids() if attempt else {}
            log.exception(f"error while {doing}; trying again next tick", extra=fields(**ids))

    # -- 1. receive -----------------------------------------------------------------------

    def _receive(self, now: float) -> None:
        free = self.settings.capacity - len(self.attempts)
        if free <= 0:
            return
        for message in self.queue.receive(min(free, 10)):
            try:
                request = MatchRequest.parse(message.body)
            except RequestError as e:
                # Left undeleted: after a few receives, the queue moves it to its dead-letter queue.
                log.error("bad match request", extra=fields(reason=str(e)))
                continue
            key = (request.match_id, request.attempt)
            if key in self.attempts:
                # Delivered again (at least once): the newest receipt is the valid one.
                self.attempts[key].message = message
                continue
            attempt = Attempt(
                request=request,
                message=message,
                secret=Secrets.new(),
                received_at=now,
                visible_until=now + self.settings.visibility_s,
                on_demand=[request.ranked, request.ranked],
            )
            self.attempts[key] = attempt
            with self._guard("starting a match", attempt):
                self._start(attempt, now)

    # -- 2. start or adopt ----------------------------------------------------------------

    def _start(self, attempt: Attempt, now: float) -> None:
        match_id, number = attempt.key
        if self.store.read_status(match_id, number) is not None:
            log.info("attempt already reported; dropping the copy", extra=fields(**attempt.ids()))
            self.queue.delete(attempt.message)
            del self.attempts[attempt.key]
            return
        existing = self.tasks.describe([attempt.key]).get(attempt.key, {})
        if not existing:
            self._request_controller(attempt)
        elif set(existing) == {Role.CONTROLLER, Role.BOT0, Role.BOT1}:
            # A dispatcher crashed and its message came back: watch its tasks. Never relaunch a
            # bot, because the controller expects the tokens of the crashed dispatcher.
            attempt.phase = Phase.STARTING_BOTS
            attempt.bots_started_at = now
            attempt.controller_requested = True
            attempt.on_demand = [True, True]
            log.warning(
                "adopting the tasks of an earlier dispatcher", extra=fields(**attempt.ids())
            )
        else:
            # Its tokens died with the crashed dispatcher, so the missing bots can't be started.
            log.warning(
                "an earlier dispatcher left this attempt half-started",
                extra=fields(**attempt.ids(), tasks=sorted(existing)),
            )
            self._void(attempt, "dispatcher_restarted", now)

    def _request_controller(self, attempt: Attempt) -> None:
        spec = build_spec(attempt.request, attempt.secret, self.settings.limits)
        self.tasks.start_controller(*attempt.key, spec)
        attempt.controller_requested = True
        log.info("starting the controller", extra=fields(**attempt.ids()))

    # -- 3. watch -------------------------------------------------------------------------

    def _advance(self, now: float) -> None:
        if not self.attempts:
            return
        described = self.tasks.describe(list(self.attempts))
        for key in list(self.attempts):
            attempt = self.attempts.get(key)
            if attempt is not None:
                with self._guard("advancing a match", attempt):
                    self._step(attempt, described.get(key, {}), now)

    def _step(self, attempt: Attempt, states: dict[Role, TaskState], now: float) -> None:
        controller = states.get(Role.CONTROLLER)
        bots = [states.get(Role.BOT0), states.get(Role.BOT1)]

        stopped = [s for s in (controller, *bots) if s is not None and s.status is Status.STOPPED]
        if any(s.stop_reason is StopReason.INFRASTRUCTURE for s in stopped):
            self._void(attempt, "infrastructure", now)
            return
        if any(
            b is not None and b in stopped and b.stop_reason is StopReason.STOPPED for b in bots
        ):
            # An operator or the reaper stopped a bot: not the contestant's doing, so no loss.
            self._void(attempt, "stopped", now)
            return

        if attempt.phase is Phase.STARTING_CONTROLLER:
            if controller is None and not attempt.controller_requested:
                self._request_controller(attempt)  # the first submission failed
            elif controller is not None and controller.status is Status.STOPPED:
                self._finish(attempt, controller, now)  # e.g. a bad spec: exit code 2
            elif (
                controller is not None
                and controller.status is Status.RUNNING
                and controller.address
            ):
                self._start_bots(attempt, controller.address, now)
            elif now - attempt.received_at >= self.settings.bot_start_deadline_s:
                self._void(attempt, "controller_never_started", now)
            return

        if controller is None:
            self._void(attempt, "controller_lost", now)
            return
        if controller.status is Status.STOPPED:
            self._finish(attempt, controller, now)
            return

        if attempt.phase is Phase.STARTING_BOTS:
            if all(_started(b) for b in bots):
                attempt.phase = Phase.RUNNING
                log.info("both bots running", extra=fields(**attempt.ids()))
                return
            waited = now - attempt.bots_started_at
            if waited >= self.settings.bot_start_deadline_s:
                self._void(attempt, "bot_never_started", now)
                return
            if waited >= self.settings.spot_fallback_s:
                for slot, bot in enumerate(bots):
                    if not _started(bot) and not attempt.on_demand[slot]:
                        self._launch_bot(attempt, slot, controller.address or "", on_demand=True)
                        log.warning(
                            "bot not running on Spot; relaunching it on-demand",
                            extra=fields(**attempt.ids(), slot=slot),
                        )
            return

        limits = effective_limits(self.settings.limits)
        budget = (limits["connect_ms"] + limits["total_ms"]) / 1000 + self.settings.stuck_margin_s
        if now - attempt.received_at >= budget:
            log.error("controller stuck past its time limits", extra=fields(**attempt.ids()))
            self._void(attempt, "stuck", now, controller_error=True)

    def _start_bots(self, attempt: Attempt, address: str, now: float) -> None:
        # If a launch fails, the phase stays and the next tick tries both again (idempotent).
        for slot in (0, 1):
            self._launch_bot(attempt, slot, address, on_demand=attempt.on_demand[slot])
        attempt.phase = Phase.STARTING_BOTS
        attempt.bots_started_at = now
        log.info("starting the bots", extra=fields(**attempt.ids()))

    def _launch_bot(self, attempt: Attempt, slot: int, address: str, *, on_demand: bool) -> None:
        self.tasks.start_bot(
            attempt.request.match_id,
            attempt.request.attempt,
            slot,
            attempt.request.submissions[slot],
            address,
            attempt.secret.tokens[slot],
            on_demand=on_demand,
        )
        attempt.on_demand[slot] = on_demand

    # -- decide ---------------------------------------------------------------------------

    def _finish(self, attempt: Attempt, controller: TaskState, now: float) -> None:
        if controller.exit_code == 2:
            log.error(
                "ALERT: the controller rejected its configuration; not retrying",
                extra=fields(**attempt.ids()),
            )
            self._void(attempt, "bad_config", now, retry_allowed=False)
            return
        result = self._read_result(attempt)
        if result is None:
            self._void(attempt, "controller_error", now, controller_error=True)
        elif result.status == "completed" and controller.exit_code == 0:
            message = completed_message(
                attempt.request, result, self.store.replay_uri(*attempt.key)
            )
            self._report(attempt, message, retry=False, controller_error=False, now=now)
        elif result.status == "aborted":
            self._void(attempt, "aborted", now)
        else:
            self._void(attempt, "controller_error", now, controller_error=True)

    def _read_result(self, attempt: Attempt) -> MatchResult | None:
        text = self.store.read_result(*attempt.key)
        if text is None:
            log.error("the controller wrote no result", extra=fields(**attempt.ids()))
            return None
        try:
            result = parse_result(text)
        except ResultError as e:
            log.error("unreadable result", extra=fields(**attempt.ids(), reason=str(e)))
            return None
        if (result.match_id, result.attempt) != attempt.key:
            log.error("the result belongs to another attempt", extra=fields(**attempt.ids()))
            return None
        return result

    def _void(
        self,
        attempt: Attempt,
        reason: str,
        now: float,
        *,
        retry_allowed: bool = True,
        controller_error: bool = False,
    ) -> None:
        retry = retry_allowed and attempt.request.attempt < MAX_ATTEMPTS
        if retry_allowed and not retry:
            log.error(
                "ALERT: giving up on this match after the last attempt",
                extra=fields(**attempt.ids(), reason=reason),
            )
        message = voided_message(attempt.request, reason, retry=retry)
        self._report(attempt, message, retry=retry, controller_error=controller_error, now=now)

    # -- report ---------------------------------------------------------------------------

    def _report(
        self,
        attempt: Attempt,
        message: dict[str, Any],
        *,
        retry: bool,
        controller_error: bool,
        now: float,
    ) -> None:
        match_id, number = attempt.key
        self.results.send(message)
        self.store.write_status(
            match_id,
            number,
            {
                "status": message["status"],
                "reason": message.get("reason"),
                "submissions": dict(enumerate(attempt.request.submissions)),
                "occurred_at": self.clock.wall(),
            },
        )
        self.tasks.stop(match_id, number)
        if retry:
            self.queue.send(attempt.request.next_attempt().to_json(), self.settings.retry_delay_s)
        self.queue.delete(attempt.message)
        del self.attempts[attempt.key]
        log.info(
            "attempt reported",
            extra=fields(
                **attempt.ids(),
                status=message["status"],
                reason=message.get("reason"),
                retry=retry,
            ),
        )
        if self.breaker.record(now, controller_error=controller_error):
            errors, total = self.breaker.error_rate(now)
            log.error(
                "ALERT: circuit breaker tripped; taking no new matches until an operator resets it",
                extra=fields(controller_errors=errors, finished=total),
            )

    # -- 4. heartbeat ---------------------------------------------------------------------

    def _heartbeat(self, now: float) -> None:
        for attempt in self.attempts.values():
            if attempt.visible_until - now < self.settings.visibility_s / 2:
                with self._guard("extending a message", attempt):
                    self.queue.extend(attempt.message, self.settings.visibility_s)
                    attempt.visible_until = now + self.settings.visibility_s
