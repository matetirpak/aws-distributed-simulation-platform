"""The seams between the dispatcher's logic and the world it drives.

The core (`core.py`) only talks to these interfaces. `local.py` implements them with docker
compose and local files; the AWS adapters (ECS, SQS, S3) come with the execution account. Tests
use the fakes in `tests/fakes.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class Role(StrEnum):
    CONTROLLER = "controller"
    BOT0 = "bot0"
    BOT1 = "bot1"

    @staticmethod
    def bot(slot: int) -> Role:
        return (Role.BOT0, Role.BOT1)[slot]


class Status(StrEnum):
    PENDING = "pending"  # requested, not running yet (image pull, capacity, placement)
    RUNNING = "running"
    STOPPED = "stopped"


class StopReason(StrEnum):
    EXITED = "exited"  # the process ended on its own; see `exit_code`
    INFRASTRUCTURE = "infrastructure"  # Spot reclaim, host failure, task retirement
    STOPPED = "stopped"  # we (or an operator) stopped it


@dataclass(frozen=True)
class TaskState:
    status: Status
    #: Where bots reach the controller (`host:port`), once it's running.
    address: str | None = None
    exit_code: int | None = None
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class Message:
    """A message received from a queue. `receipt` identifies this delivery of it."""

    receipt: str
    body: str


class Queue(Protocol):
    """The match queue (SQS on AWS). At-least-once delivery, with visibility timeouts."""

    def receive(self, max_messages: int) -> list[Message]: ...

    def extend(self, message: Message, seconds: int) -> None:
        """Keeps `message` invisible to other consumers for `seconds` from now."""

    def delete(self, message: Message) -> None: ...

    def send(self, body: str, delay_seconds: int) -> None: ...


class Tasks(Protocol):
    """Starts, watches and stops the three tasks of one attempt (ECS on AWS).

    Starting is idempotent per `(match_id, attempt, role)`: starting a task that already exists
    does nothing. That's what lets a dispatcher adopt the tasks of one that crashed.
    """

    def start_controller(self, match_id: str, attempt: int, spec: str) -> None: ...

    def start_bot(
        self,
        match_id: str,
        attempt: int,
        slot: int,
        submission: str,
        controller_address: str,
        token: str,
        *,
        on_demand: bool,
    ) -> None:
        """Starts the bot, or replaces a pending Spot bot with an on-demand one."""

    def describe(self, keys: list[tuple[str, int]]) -> dict[tuple[str, int], dict[Role, TaskState]]:
        """The tasks of each `(match_id, attempt)`; tasks that don't exist are missing."""

    def stop(self, match_id: str, attempt: int) -> None:
        """Stops and cleans up whatever is left of the attempt. Idempotent."""


class Store(Protocol):
    """The replay bucket (S3 on AWS): `replays/<match_id>/<attempt>/`."""

    def read_result(self, match_id: str, attempt: int) -> str | None:
        """The controller's `result.json`, or None if it wasn't written."""

    def read_status(self, match_id: str, attempt: int) -> dict[str, Any] | None:
        """Our `status.json`: present once the attempt was reported, so it's never played twice."""

    def write_status(self, match_id: str, attempt: int, status: dict[str, Any]) -> None: ...

    def replay_uri(self, match_id: str, attempt: int) -> str: ...


class Results(Protocol):
    """The backend's results queue (SQS in the backend's account on AWS)."""

    def send(self, message: dict[str, Any]) -> None: ...


class Control(Protocol):
    """Operator controls (an SSM parameter on AWS)."""

    def breaker_reset_requested(self) -> bool:
        """True once per reset an operator requested."""


class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds, for deadlines."""

    def wall(self) -> float:
        """Unix time, for records."""
