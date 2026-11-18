"""The local backend: the same dispatcher logic, with docker compose instead of ECS, a folder
instead of S3, and an in-memory queue instead of SQS.

Each attempt is its own compose project (`pgd-<match>-<attempt>`) from the repo's
`compose.yaml`, started in the order ECS will use: the controller first, then each bot once the
controller runs. A submission is a bot mode (`ok`, `slow`, `crash`, ... as in `bots/python/bot.py`);
`absent` never starts a bot, which exercises the start deadline and the retries.

Outputs: `<out>/<match>/<attempt>/` holds `replay.json.gz`, `result.json` (the controller's),
`status.json` (ours) and `compose.log`; `<out>/results.jsonl` gets every results message.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ports import Message, Role, Status, StopReason, TaskState

#: The platform/ directory: `compose.yaml` and the Dockerfile live there.
REPO = Path(__file__).resolve().parents[2]

ABSENT = "absent"


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


@dataclass
class _Entry:
    id: int
    body: str
    visible_at: float
    receives: int = 0


class LocalQueue:
    """In memory, with SQS's visibility timeout and delayed sends."""

    def __init__(self, visibility_s: int = 120) -> None:
        self.visibility_s = visibility_s
        self._entries: list[_Entry] = []
        self._next = 1

    def put(self, body: str, delay_seconds: int = 0) -> None:
        self._entries.append(_Entry(self._next, body, time.monotonic() + delay_seconds))
        self._next += 1

    def empty(self) -> bool:
        return not self._entries

    def receive(self, max_messages: int) -> list[Message]:
        now = time.monotonic()
        out: list[Message] = []
        for entry in self._entries:
            if len(out) == max_messages:
                break
            if entry.visible_at <= now:
                entry.receives += 1
                entry.visible_at = now + self.visibility_s
                out.append(Message(f"{entry.id}#{entry.receives}", entry.body))
        return out

    def _find(self, message: Message) -> _Entry | None:
        entry_id = int(message.receipt.split("#")[0])
        return next((e for e in self._entries if e.id == entry_id), None)

    def extend(self, message: Message, seconds: int) -> None:
        entry = self._find(message)
        if entry is not None:
            entry.visible_at = time.monotonic() + seconds

    def delete(self, message: Message) -> None:
        entry = self._find(message)
        if entry is not None:
            self._entries.remove(entry)

    def send(self, body: str, delay_seconds: int) -> None:
        self.put(body, delay_seconds)


def project_name(match_id: str, attempt: int) -> str:
    return "pgd-" + re.sub(r"[^a-z0-9_-]", "-", match_id.lower()) + f"-{attempt}"


class ComposeTasks:
    """Runs each role as a compose service. Every start is idempotent: an existing container is
    left alone, which is how a restarted dispatcher adopts running matches."""

    def __init__(
        self,
        out_root: Path,
        *,
        mock_game: str,
        controller_image: str | None = None,
        repo: Path = REPO,
    ) -> None:
        self.out_root = out_root
        self.mock_game = mock_game
        self.controller_image = controller_image
        self.repo = repo

    def build(self) -> None:
        """Builds the images once, before any match. An external controller image is used as is."""
        services = ["bot0"] if self.controller_image else []
        self._compose("pgd-build", self._env(self.out_root), "build", "--quiet", *services)

    def start_controller(self, match_id: str, attempt: int, spec: str) -> None:
        if Role.CONTROLLER in self._states(match_id, attempt):
            return
        out = self._out(match_id, attempt)
        out.mkdir(parents=True, exist_ok=True)
        # The spec holds the tokens: it goes in the environment, never on a command line.
        env = self._env(out, MATCH_SPEC=spec)
        self._up(project_name(match_id, attempt), env, "controller")

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
        # Locally there's no Spot, and compose resolves the controller by its service name.
        del controller_address, on_demand
        if submission == ABSENT or Role.bot(slot) in self._states(match_id, attempt):
            return
        env = self._env(
            self._out(match_id, attempt), **{f"TOKEN{slot}": token, f"MODE{slot}": submission}
        )
        self._up(project_name(match_id, attempt), env, f"bot{slot}")

    def describe(self, keys: list[tuple[str, int]]) -> dict[tuple[str, int], dict[Role, TaskState]]:
        described = {}
        for key in keys:
            states = self._states(*key)
            if states:
                described[key] = states
        return described

    def stop(self, match_id: str, attempt: int) -> None:
        project = project_name(match_id, attempt)
        out = self._out(match_id, attempt)
        env = self._env(out)
        logs = self._compose(project, env, "logs", "--no-color", check=False)
        if logs and out.is_dir():
            (out / "compose.log").write_text(logs)
        self._compose(
            project, env, "down", "--volumes", "--remove-orphans", "--timeout", "2", check=False
        )

    # -- helpers --------------------------------------------------------------------------

    def _out(self, match_id: str, attempt: int) -> Path:
        return self.out_root / match_id / str(attempt)

    def _states(self, match_id: str, attempt: int) -> dict[Role, TaskState]:
        project = project_name(match_id, attempt)
        text = self._compose(
            project, self._env(self._out(match_id, attempt)), "ps", "--all", "--format", "json"
        )
        states: dict[Role, TaskState] = {}
        for container in _parse_ps(text):
            try:
                role = Role(container.get("Service", ""))
            except ValueError:
                continue
            states[role] = _state(role, container)
        return states

    def _env(self, out: Path, **values: str) -> dict[str, str]:
        # compose.yaml requires every variable, even for services we don't start; placeholders
        # fill the ones a given call doesn't need.
        env = {
            **os.environ,
            "MATCH_SPEC": "{}",
            "MOCK_GAME": self.mock_game,
            "TOKEN0": "unused",
            "TOKEN1": "unused",
            "MODE0": "ok",
            "MODE1": "ok",
            "OUT_DIR": str(out),
            "HOST_UID": str(os.getuid()),
            "HOST_GID": str(os.getgid()),
        }
        if self.controller_image:
            env["CONTROLLER_IMAGE"] = self.controller_image
        env.update(values)
        return env

    def _up(self, project: str, env: dict[str, str], service: str) -> None:
        self._compose(
            project, env, "up", "--detach", "--no-build", "--no-deps", "--no-recreate",
            "--quiet-pull", service,
        )  # fmt: skip

    def _compose(self, project: str, env: dict[str, str], *args: str, check: bool = True) -> str:
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.repo),
            "--project-name",
            project,
        ]
        done = subprocess.run(
            [*command, *args], env=env, capture_output=True, text=True, check=False, timeout=600
        )
        if check and done.returncode != 0:
            # stderr is docker's; it never contains the environment, so no token can leak here.
            raise RuntimeError(f"docker compose {args[0]} failed: {done.stderr.strip()[-500:]}")
        return done.stdout


def _parse_ps(text: str) -> list[dict[str, Any]]:
    """`compose ps --format json`: an object per line (current versions) or an array (older)."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _state(role: Role, container: dict[str, Any]) -> TaskState:
    state = str(container.get("State", "")).lower()
    if state == "running":
        address = "controller:9000" if role is Role.CONTROLLER else None
        return TaskState(Status.RUNNING, address=address)
    if state in ("exited", "dead"):
        code = container.get("ExitCode")
        return TaskState(
            Status.STOPPED,
            exit_code=code if isinstance(code, int) else None,
            stop_reason=StopReason.EXITED,
        )
    return TaskState(Status.PENDING)


class DirStore:
    def __init__(self, out_root: Path) -> None:
        self.out_root = out_root

    def _dir(self, match_id: str, attempt: int) -> Path:
        return self.out_root / match_id / str(attempt)

    def read_result(self, match_id: str, attempt: int) -> str | None:
        path = self._dir(match_id, attempt) / "result.json"
        return path.read_text() if path.is_file() else None

    def read_status(self, match_id: str, attempt: int) -> dict[str, Any] | None:
        path = self._dir(match_id, attempt) / "status.json"
        if not path.is_file():
            return None
        status: dict[str, Any] = json.loads(path.read_text())
        return status

    def write_status(self, match_id: str, attempt: int, status: dict[str, Any]) -> None:
        directory = self._dir(match_id, attempt)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "status.json").write_text(json.dumps(status, indent=2) + "\n")

    def replay_uri(self, match_id: str, attempt: int) -> str:
        return (self._dir(match_id, attempt) / "replay.json.gz").resolve().as_uri()


class FileResults:
    """Appends each results message as one line, standing in for the backend's results queue."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def send(self, message: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(message) + "\n")


class FileControl:
    """`touch <file>` resets a tripped circuit breaker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen = self._mtime()

    def _mtime(self) -> float | None:
        return self.path.stat().st_mtime if self.path.exists() else None

    def breaker_reset_requested(self) -> bool:
        mtime = self._mtime()
        if mtime is not None and mtime != self._seen:
            self._seen = mtime
            return True
        return False
