"""In-memory stand-ins for the ports, so the core's decisions can be tested tick by tick."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dispatcher.breaker import CircuitBreaker
from dispatcher.core import Dispatcher, Settings
from dispatcher.ports import Message, Role, Status, StopReason, TaskState

Key = tuple[str, int]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return 1_760_000_000.0 + self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _Entry:
    id: int
    body: str
    visible_at: float
    receives: int = 0


class FakeQueue:
    """An SQS-like queue: received messages stay invisible for `visibility_s`, then reappear."""

    def __init__(self, clock: FakeClock, visibility_s: int = 120) -> None:
        self.clock = clock
        self.visibility_s = visibility_s
        self.entries: list[_Entry] = []
        self.sent: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.extended: list[tuple[str, int]] = []
        self._next = 1

    def put(self, body: str, delay: int = 0) -> None:
        self.entries.append(_Entry(self._next, body, self.clock.now + delay))
        self._next += 1

    def receive(self, max_messages: int) -> list[Message]:
        out: list[Message] = []
        for entry in self.entries:
            if len(out) == max_messages:
                break
            if entry.visible_at <= self.clock.now:
                entry.receives += 1
                entry.visible_at = self.clock.now + self.visibility_s
                out.append(Message(f"{entry.id}#{entry.receives}", entry.body))
        return out

    def _entry(self, message: Message) -> _Entry | None:
        entry_id = int(message.receipt.split("#")[0])
        return next((e for e in self.entries if e.id == entry_id), None)

    def extend(self, message: Message, seconds: int) -> None:
        self.extended.append((message.receipt, seconds))
        entry = self._entry(message)
        if entry is not None:
            entry.visible_at = self.clock.now + seconds

    def delete(self, message: Message) -> None:
        self.deleted.append(message.receipt)
        entry = self._entry(message)
        if entry is not None:
            self.entries.remove(entry)

    def send(self, body: str, delay_seconds: int) -> None:
        self.sent.append((body, delay_seconds))
        self.put(body, delay_seconds)


@dataclass
class BotStart:
    key: Key
    slot: int
    submission: str
    address: str
    token: str
    on_demand: bool


class FakeTasks:
    def __init__(self) -> None:
        self.states: dict[Key, dict[Role, TaskState]] = {}
        self.specs: dict[Key, str] = {}
        self.bot_starts: list[BotStart] = []
        self.stopped: list[Key] = []
        #: Failure injection: the next controller start raises; bot starts of this match raise.
        self.fail_next_controller_start = False
        self.fail_bot_starts_for: str | None = None

    # The ports

    def start_controller(self, match_id: str, attempt: int, spec: str) -> None:
        if self.fail_next_controller_start:
            self.fail_next_controller_start = False
            raise RuntimeError("ThrottlingException")
        tasks = self.states.setdefault((match_id, attempt), {})
        if Role.CONTROLLER not in tasks:
            tasks[Role.CONTROLLER] = TaskState(Status.PENDING)
            self.specs[(match_id, attempt)] = spec

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
        if self.fail_bot_starts_for == match_id:
            raise RuntimeError("ThrottlingException")
        key = (match_id, attempt)
        self.bot_starts.append(
            BotStart(key, slot, submission, controller_address, token, on_demand)
        )
        self.states.setdefault(key, {})[Role.bot(slot)] = TaskState(Status.PENDING)

    def describe(self, keys: list[Key]) -> dict[Key, dict[Role, TaskState]]:
        return {k: dict(self.states[k]) for k in keys if k in self.states}

    def stop(self, match_id: str, attempt: int) -> None:
        key = (match_id, attempt)
        self.stopped.append(key)
        for role, state in self.states.get(key, {}).items():
            if state.status is not Status.STOPPED:
                self.states[key][role] = TaskState(
                    Status.STOPPED, exit_code=143, stop_reason=StopReason.STOPPED
                )

    # What the world does between ticks

    def running(self, key: Key, role: Role, address: str | None = None) -> None:
        self.states.setdefault(key, {})[role] = TaskState(Status.RUNNING, address=address)

    def exited(self, key: Key, role: Role, code: int) -> None:
        self.states[key][role] = TaskState(
            Status.STOPPED, exit_code=code, stop_reason=StopReason.EXITED
        )

    def stop_one(self, key: Key, role: Role) -> None:
        self.states[key][role] = TaskState(
            Status.STOPPED, exit_code=143, stop_reason=StopReason.STOPPED
        )

    def reclaimed(self, key: Key, role: Role) -> None:
        self.states[key][role] = TaskState(
            Status.STOPPED, exit_code=137, stop_reason=StopReason.INFRASTRUCTURE
        )

    def tokens_in_spec(self, key: Key) -> list[str]:
        return [p["token"] for p in json.loads(self.specs[key])["players"]]


class FakeStore:
    def __init__(self) -> None:
        self.results: dict[Key, str] = {}
        self.statuses: dict[Key, dict[str, Any]] = {}

    def read_result(self, match_id: str, attempt: int) -> str | None:
        return self.results.get((match_id, attempt))

    def read_status(self, match_id: str, attempt: int) -> dict[str, Any] | None:
        return self.statuses.get((match_id, attempt))

    def write_status(self, match_id: str, attempt: int, status: dict[str, Any]) -> None:
        self.statuses[(match_id, attempt)] = status

    def replay_uri(self, match_id: str, attempt: int) -> str:
        return f"mem://replays/{match_id}/{attempt}/replay.json.gz"


class FakeResults:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


class FakeControl:
    def __init__(self) -> None:
        self.reset = False

    def breaker_reset_requested(self) -> bool:
        requested, self.reset = self.reset, False
        return requested


@dataclass
class World:
    clock: FakeClock
    queue: FakeQueue
    tasks: FakeTasks
    store: FakeStore
    results: FakeResults
    control: FakeControl
    dispatcher: Dispatcher = field(repr=False)


def world(settings: Settings | None = None, breaker: CircuitBreaker | None = None) -> World:
    clock = FakeClock()
    queue = FakeQueue(clock)
    tasks = FakeTasks()
    store = FakeStore()
    results = FakeResults()
    control = FakeControl()
    dispatcher = Dispatcher(
        queue=queue,
        tasks=tasks,
        store=store,
        results=results,
        control=control,
        clock=clock,
        settings=settings,
        breaker=breaker,
    )
    return World(clock, queue, tasks, store, results, control, dispatcher)


def request(match_id: str = "m-1", attempt: int | None = None, *, ranked: bool = False) -> str:
    body: dict[str, Any] = {
        "match_id": match_id,
        "submissions": ["sub-a", "sub-b"],
        "ranked": ranked,
        "game": {"map": "arena-3"},
    }
    if attempt is not None:
        body["attempt"] = attempt
    return json.dumps(body)


def result_json(
    match_id: str, attempt: int, status: str = "completed", outcomes: tuple[str, str] = ("ok", "ok")
) -> str:
    return json.dumps(
        {
            "match_id": match_id,
            "attempt": attempt,
            "status": status,
            "turns": 42,
            "controller_version": "0.1.0",
            "game_version": None,
            "players": [
                {
                    "slot": slot,
                    "rank": slot + 1,
                    "score": 10 - slot,
                    "outcome": outcome,
                    "timeouts": 0,
                    "invalid_actions": 0,
                }
                for slot, outcome in enumerate(outcomes)
            ],
        }
    )
