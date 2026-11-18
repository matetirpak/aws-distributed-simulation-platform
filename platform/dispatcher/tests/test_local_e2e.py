"""Real matches through the dispatcher, with docker compose: the mock controller and the Python
bot, in three scenarios. Slow (about a minute) and needs Docker, so it runs only on request:
`uv run pytest -m docker`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatcher.__main__ import main

pytestmark = pytest.mark.docker


def test_three_matches_through_the_dispatcher(tmp_path: Path) -> None:
    code = main(
        [
            "local",
            "--game", "coin",
            "--match", "ok,ok",
            "--match", "ok,absent",
            "--match", "ok,crash",
            "--out", str(tmp_path),
            "--bot-start-deadline", "15",
            "--poll", "0.5",
        ]
    )  # fmt: skip
    assert code == 0
    messages = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    by_match: dict[str, list[dict[str, object]]] = {}
    for message in messages:
        by_match.setdefault(str(message["match_id"])[-1], []).append(message)

    [ok] = by_match["0"]
    assert ok["status"] == "completed"

    absent = sorted(by_match["1"], key=lambda m: int(str(m["attempt"])))
    assert [(m["status"], m["reason"], m["retry"]) for m in absent] == [
        ("voided", "bot_never_started", True),
        ("voided", "bot_never_started", True),
        ("voided", "bot_never_started", False),
    ]

    [crash] = by_match["2"]
    assert crash["status"] == "completed", "a bot that crashes by itself loses; nothing is voided"
    result = crash["result"]
    assert isinstance(result, dict)
    assert [p["outcome"] for p in result["players"]] == ["ok", "disconnected"]
