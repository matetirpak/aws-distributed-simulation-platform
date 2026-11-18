"""The contract with the Rust side: a match spec built here, run through the real controller, and
the `result.json` that controller writes, read back here.

Needs the controller binary: `cargo build -p mock-controller`, then
`CONTROLLER_BIN=../target/debug/mock-controller uv run pytest -m contract`. CI runs this.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from dispatcher.model import MatchRequest, Secrets, build_spec, parse_result

BIN = os.environ.get("CONTROLLER_BIN", "")

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not BIN, reason="set CONTROLLER_BIN to the mock-controller binary"),
]


def run_controller(spec: str, out: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "MATCH_SPEC": spec,
        "MOCK_GAME": "coin",
        "PORT": "0",  # any free port: nobody connects anyway
        "OUT_DIR": str(out),
    }
    return subprocess.run([BIN], env=env, capture_output=True, text=True, timeout=60, check=False)


def test_the_controller_accepts_our_spec_and_we_read_its_result(tmp_path: Path) -> None:
    request = MatchRequest("contract-1", ("sub-a", "sub-b"))
    secret = Secrets.new()
    done = run_controller(build_spec(request, secret, {"connect_ms": 500}), tmp_path)

    assert done.returncode == 0, done.stdout[-2000:]
    result = parse_result((tmp_path / "result.json").read_text())
    assert (result.match_id, result.attempt, result.status) == ("contract-1", 1, "completed")
    assert [p.outcome for p in result.players] == ["never_connected", "never_connected"]
    assert (tmp_path / "replay.json.gz").is_file()
    assert not any(t in done.stdout + done.stderr for t in secret.tokens), "no token in its logs"


def test_a_spec_the_controller_rejects_exits_2(tmp_path: Path) -> None:
    """The negative control: if this passed with exit 0, the test above would prove nothing."""
    spec = json.loads(build_spec(MatchRequest("contract-2", ("a", "b")), Secrets.new()))
    spec["unexpected"] = 1
    done = run_controller(json.dumps(spec), tmp_path)

    assert done.returncode == 2
    assert not (tmp_path / "result.json").exists()
