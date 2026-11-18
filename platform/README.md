# platform/

The match execution code: the game controller, the relay in every bot container, two toy games that stand in for a real one, the prototype that plays matches on AWS, the dispatcher that turns match requests into matches (Python), and a local runner that plays one match with Docker. The design is described in [`docs/`](../docs).

**Status:**
- **Matches on AWS** (prototype). Each match is a task that runs a game's controller, and starts each bot in a Fargate task or on an EC2 machine of its own. Games plug in as controller images, bots as bot images; the toy games and the sample bot are the default. Results go to Postgres, replays to S3. Infrastructure: [`infra/envs/dev/`](../infra/envs/dev).
- **Locally**, two toy games stand in for the game, and the dispatcher plays matches with docker compose. Its AWS adapters (ECS, SQS, S3) aren't built yet.

```
local-runner ──spec + tokens──▶ controller (distroless, 5 MB) ◀── TCP 9000 ── relay ⇄ stdin/stdout ⇄ bot.py
                                 │ replay.json.gz, result.json          (one container per bot, own network)
                                 ▼
                              out/<match_id>/
```

| To… | Read |
|---|---|
| understand what runs and how a local match flows, step by step, without knowing Rust | [how-it-works.md](../docs/how-it-works.md) |
| run matches on AWS | [architecture](../docs/architecture.md), and the [quick start](../README.md) |

## Layout

Rust code is organized in *crates* (packages), in one Cargo workspace. Three of them are programs you can run; the others are libraries those programs use.

| Path | Kind | In plain words | On AWS it becomes |
|---|---|---|---|
| `crates/protocol` | library | The message formats every side agrees on | Shared by every component |
| `crates/controller` | library | The game server's logic: accept bots, check tokens, send turns, time answers, write results | Part of the game server image |
| `crates/games` | library | Two toy games (coin, Nim) standing in for the real game, plus broken games for tests | A game's code |
| `crates/mock-controller` | **program** | The game server: picks a toy game and runs the controller logic. Its `tests/` start it and play against it | The game server image (Fargate task) |
| `crates/relay` | **program** | Runs in each bot container: connects to the game server, starts the bot, carries messages through the bot's input and output | `/relay` in every bot image |
| `crates/local-runner` | **program** | Your laptop tool: creates one match, starts the 3 containers, prints the result and checks the replay | A game's smoke match |
| `dispatcher/` | Python | Takes match requests, starts the controller and the bots, voids and retries infrastructure failures, reports results. Runs locally with docker compose | The dispatcher service (Fargate) |
| `prototype/` | Python, Docker | The match task's entrypoint, the isolation probe, the match image (a game's controller plus the entrypoint), the sample bot's image, `push-image.sh` and `run_batch.py` | The prototype on AWS ([architecture](../docs/architecture.md)) |
| `bots/python/bot.py` | Python | A sample bot, with modes that misbehave on purpose | A contestant's bot |
| `Dockerfile` | | The local images: the mock game server, and Python + relay + bot | |
| `compose.yaml` | | Starts one match: the game server plus two bots, each bot on its own private network | ECS tasks and security groups |

## Setup

1. [rustup](https://rustup.rs). `rust-toolchain.toml` selects the exact Rust version on the first `cargo` command.
2. Docker with Compose v2 (tested with Docker 29.5 and Compose 5.1), and `python3` for the relay tests.
3. For the dispatcher: [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and the locked tools on the first `uv sync`.

## Test

```bash
cargo fmt --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked       # ~10 s: 15 protocol tests, 11 relay tests, unit tests
```

The protocol tests run the real controller binary against scripted fake bots over localhost TCP; the relay tests run the real relay between a fake controller and small Python bots. CI runs the same three commands on every PR that touches `platform/` (`../.github/workflows/ci.yml`).

The dispatcher, from `dispatcher/`:

```bash
uv sync --locked
uv run ruff check && uv run ruff format --check && uv run mypy   # lint, format, strict types
uv run pytest                                                     # ~1 s: the scenarios with fakes
cargo build -p mock-controller && CONTROLLER_BIN=../target/debug/mock-controller uv run pytest -m contract
uv run pytest -m docker                                           # ~1 min: real matches, needs Docker
```

The contract tests run a match spec built by the dispatcher through the real controller, and parse the `result.json` it writes, so Python and Rust can't drift apart. CI runs everything except the docker test. If your shell sets `PYTHONPATH` (ROS does), prefix the commands with `env -u PYTHONPATH`, or pytest loads foreign plugins.

## Run matches through the dispatcher

```bash
cd dispatcher
uv run python -m dispatcher local --game coin --match ok,ok --match ok,absent --match ok,crash
```

Each `--match` is one match request with two submissions; locally, a submission is a bot mode from the table below. The dispatcher starts the controller, then both bots, watches them, and decides:

| Match | Results message(s) | Why |
|---|---|---|
| `ok,ok` | `completed` | |
| `ok,crash` | `completed`, slot 1 `disconnected` | A bot that crashes on its own loses; nothing is voided |
| `ok,absent` | `voided bot_never_started` 3 times, the last with `retry: false` | The second bot never starts: an infrastructure failure, retried up to 3 attempts |

Results go to `out/dispatcher/results.jsonl` (standing in for the backend's results queue); each attempt's replay, `result.json`, `status.json` and compose log go to `out/dispatcher/<match>/<attempt>/`. `touch out/dispatcher/reset-breaker` resets a tripped circuit breaker. How it decides, step by step: [how-it-works.md](../docs/how-it-works.md#the-dispatcher).

**Updating its tools:** `pyproject.toml` resolves only releases older than its `exclude-newer` date (our 7-day rule). Move the date to 7 days ago, run `uv lock --upgrade`, and review the diff. uv itself is pinned in `../.github/tools.lock`.

## Play a match

```bash
cargo run -p local-runner -- coin ok ok
cargo run -p local-runner -- nim ok garbage
cargo run -p local-runner -- coin ok absent    # the second bot never starts
cargo run -p local-runner -- --controller-image <image> ok ok   # smoke match with a real game's image
```

With `--controller-image`, a real game's controller image (for example from that game's release workflow) plays against our relay and Python bot. It passes when the controller completes the match. Our bot doesn't know that game and answers `{}`, so the bots' outcomes don't matter, and the replay isn't re-simulated here.

| Bot mode | Does | Expected outcome |
|---|---|---|
| `ok` | Plays normally | `ok` |
| `slow` | Misses the budget every 5th turn | `timeout` after 3 timeouts |
| `crash` | Exits on turn 3 | `disconnected` |
| `garbage` | Non-JSON and illegal moves | `invalid_output` after 10 invalid actions |
| `huge` | A line over 64 KiB | `invalid_output` |
| `silent` | Never answers `init` | `timeout` |
| `noflush` | Never flushes stdout | `timeout` |
| `probe` | Tries the internet, instance metadata, the ECS credentials endpoint, public DNS and the other bot; plays garbage if any works | `ok` (nothing reachable) |
| `absent` | Second slot only: the bot never starts | `never_connected` |

The runner prints each bot's relay report (exit status and captured stderr), the result table and the replay check. Output, including the tokens of local matches, goes to `out/<match_id>/` (git-ignored).

## Plugging in a game

A real game implements `controller::Game` (see `crates/controller/src/game.rs`) and its binary is one line: `controller::run::<TheGame>()`. The `controller` stage of the `Dockerfile` is the reference for building it: base images pinned by digest, `cargo build --locked`, a static musl binary on distroless (non-root, no shell), one port, `ENTRYPOINT`, and source labels.

## Updating Rust

Crates, actions and base image digests are bumped by hand, in reviewed pull requests. Rust itself is bumped in four places at once: `rust-toolchain.toml`, `rust-version` in `Cargo.toml`, and the builder image in `Dockerfile` and `prototype/Dockerfile` (a version at least 7 days old, pinned by digest).
