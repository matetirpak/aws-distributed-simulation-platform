# Contracts

The exact interfaces between the components. The message types live in one shared Rust `protocol` crate used by both the controller and the relay, so the two can't drift apart. Field names here are binding. Example values are illustrative.

## Controller ⇄ relay

TCP, UTF-8, **one minified JSON object per line**. The relay dials the controller on port 9000, so bots need no inbound port. There's no HTTP and no WebSocket: plain TCP lets the controller push each turn, and lets either side send whenever the protocol says so.

| Direction | Message | When |
|---|---|---|
| relay → controller | `{"type":"hello","token":"…","protocol":1}` | Within 2 s of connecting, or the connection is dropped |
| controller → relay | `{"type":"init","slot":0,"players":2,"config":{…},"budget_ms":5000}` | Once every player has connected |
| relay → controller | `{"type":"ready"}` | |
| controller → relay | `{"type":"turn","turn":17,"state":{…},"budget_ms":2000}` | To each player who moves this turn |
| relay → controller | `{"type":"action","turn":17,"action":{…}}` or `{"type":"action","turn":17,"invalid":true}` | |
| controller → relay | `{"type":"end","rank":1,"score":84}`, then the controller closes | |

Rules:
- **Tokens are single-use.** A wrong, reused or malformed token, an unknown `protocol` version, or a non-JSON `hello` gets the connection closed. A rejected `hello` doesn't use up the token, so a bad attempt can't lock the real bot out.
- **Answers carry the turn number.** Answers to an earlier turn are ignored. An answer that arrives after budget + grace is a timeout.
- **Lines from the relay are capped at 64 KB.** A longer line means `invalid_output`, and the connection is closed.
- **A line that isn't valid JSON, or isn't a valid message, counts as one invalid action**. The relay should never send one, but a bot can take over its relay.
- **Static data** (board, rules) goes in `init`. **Every `turn` carries the full dynamic state**, not a diff, so bots stay stateless and can't drift out of sync. Prefer compact encodings, such as row strings like `["..#..", …]` rather than one object per cell.
- **Debug info** from the relay (capped stderr, resource use) needs a transport to the contestant. One option is an extra message type on this connection; it's still open.
- **Non-movers get nothing.** In alternating turns only the player to move gets a `turn`. In the replay, `actions` and `times_ms` are `null` for every slot that didn't move.

## Relay ⇄ bot

The convention from CodinGame and Halite: **the bot reads stdin and writes stdout**, so no language needs socket code.

- **The first stdin line** is the full `init` message. The bot prints any line to signal it's ready.
- **Then one `turn` line in, one line out** with the action JSON. The relay wraps the answer as `{"type":"action","turn":N,"action":…}`, or as `{"invalid":true}` if the line isn't valid JSON. It matches answers to requests in order, so a late answer keeps its own turn number.
- **The bot must flush after every line.** Python, C, C++ and Java buffer stdout when it's a pipe; this will be the most common support question. Starter kits handle it, and the relay sets `PYTHONUNBUFFERED=1`.
- **Debug output goes to stderr.** The relay captures it, capped, as debug info for the contestant.
- **stdin closing (EOF) means the match is over.** The bot should exit; the relay kills it after 2 s.

Relay behavior (a static Rust binary used as `ENTRYPOINT ["/relay", "--"]`):
1. **Before** starting the bot: connect to `CONTROLLER_ADDR`, send `hello` with `BOT_TOKEN`, then remove both variables from the bot's environment.
2. Use blocking or async reads only, and **never busy-wait**, which would steal the bot's CPU.
3. Buffers: about 16 MiB from the controller to the bot, 64 KiB from the bot to the controller.
4. Bot exits: close the socket. Controller closes: close the bot's stdin, kill the bot after 2 s, and exit.
5. A bot line over 64 KiB is passed on just past the limit, so the controller applies its own rule (`invalid_output`). Output when no request is open is dropped.
6. Before exiting, write one JSON report line to the relay's own stderr: the bot's exit status and its captured stderr (64 KiB cap). This is the contestant's debug info.

Reference implementation: `crates/relay` in [`platform/`](../platform). It uses std threads and blocking reads, with no async runtime.

Starter kits (one per language) handle the read loop and the flushing. They ship sample input files, to be tested with redirection (`python bot.py < turns.jsonl`): pasting into a terminal truncates lines at about 4 KB.

## Match spec

From the dispatcher to the controller, as **one JSON environment variable `MATCH_SPEC`** in `RunTask` `containerOverrides`:

```json
{"match_id": "m-7f3a", "attempt": 1, "seed": 918273645,
 "players": [{"slot": 0, "token": "<128-bit random>"}, {"slot": 1, "token": "<128-bit random>"}],
 "limits": {"connect_ms": 300000, "init_ms": 5000, "turn_ms": 2000, "grace_ms": 100,
            "max_turns": 500, "total_ms": 900000, "max_timeouts": 3, "max_invalid": 10},
 "game": {"map": "arena-3"}}
```

- **Players are only slots and tokens.** The controller never learns who plays; the dispatcher keeps the slot → submission mapping.
- **`limits`** are merged over the controller's defaults (the values above). The controller enforces them, and the values come from the spec. A game proposes its own defaults.
- **`game`** is passed unchanged to the game logic.
- **`seed`** is 32-bit, so JavaScript consumers don't lose precision.
- **`RunTask` overrides are capped at 8,192 characters in total.** Large data goes into the image, selected by name in `game`.
- **Where each setting lives:** game version → the image; environment (`PORT`, the replay bucket) → the task definition; per match → `MATCH_SPEC`.
- **Strict validation before listening:**
  - `match_id` is a string, `attempt` and `seed` are integers.
  - Slots are unique and run 0..n-1; tokens are unique, non-empty strings.
  - `limits` contains only known keys with positive integers; `game` is an object.

  On error, the controller exits with code 2 and names the field, without logging a token.

## Results and exit codes

| `status` | Exit | Meaning | Dispatcher |
|---|---|---|---|
| `completed` | 0 | Played to the end | Sends the result to the backend |
| `aborted` | 1 | `SIGTERM` (task stopped, Spot reclaimed, retirement) | Voids and retries |
| `controller_error` | 1 | Panic in the game logic's `apply`, or a controller bug | Voids, retries (at most 3 attempts), alerts; trips the circuit breaker when frequent |
| (none) | 2 | Bad match spec or configuration | Alerts; doesn't retry |

Per player, `outcome` is one of:

| Outcome | Meaning |
|---|---|
| `ok` | Played normally |
| `timeout` | Forfeited after `max_timeouts` turn timeouts, or no `ready` within `init_ms` |
| `disconnected` | Connection closed, or the bot stopped reading for 5 s |
| `invalid_output` | Line over 64 KB, or `max_invalid` invalid actions |
| `never_connected` | No valid `hello` within `connect_ms` |

Non-`ok` players rank below all others. **If a bot task never reached `RUNNING`, the dispatcher voids the match instead of recording `never_connected`**: an infrastructure failure never counts as a loss.

The result:

```json
{"match_id": "m-7f3a", "attempt": 1, "status": "completed", "turns": 143,
 "controller_version": "0.3.0", "game_version": "1.2.0",
 "players": [{"slot": 0, "rank": 1, "score": 84, "outcome": "ok", "timeouts": 0, "invalid_actions": 0},
             {"slot": 1, "rank": 2, "score": 61, "outcome": "timeout", "timeouts": 3, "invalid_actions": 0}]}
```

**Per attempt, the replay bucket holds three objects** under `replays/<match_id>/<attempt>/`:

| Object | Written by | Contents |
|---|---|---|
| `replay.json.gz` | Controller | The seed, `game` config, limits and versions; per turn: `movers`, the applied `actions` (`null` for none) and `times_ms` (`"timeout"` when missed); the final scores and the result |
| `result.json` | Controller, after the replay | The result above |
| `status.json` | Dispatcher, after reporting to the backend | `status` (`completed` or `voided`), `reason` for voids, the slot → `submission_id` mapping, `occurred_at` |

None of them holds tokens. The game logic re-simulated from a replay must reach the same scores.

## Results to the backend

The dispatcher sends **one message per attempt** to the backend's results queue. The queue is in the backend's account, and its policy lets the dispatcher's role send to it.

The format is a proposal, to be settled with the competition backend, whose runner contract already defines a results queue:

```json
{"match_id": "m-7f3a", "attempt": 1, "status": "completed",
 "result": {"turns": 143, "controller_version": "0.3.0", "game_version": "1.2.0", "players": [...]},
 "replay": "s3://<replay-bucket>/replays/m-7f3a/1/replay.json.gz"}
{"match_id": "m-7f3a", "attempt": 2, "status": "voided", "reason": "infrastructure", "retry": true}
```

- **Delivery is at least once.** The backend upserts by `(match_id, attempt)` and applies everything derived from a result in the same transaction.
- **`retry`** says whether the platform plays the match again (`attempt + 1`). It's `false` after the third attempt and for configuration errors: then the match won't be played on its own.
- **A `voided` after a `completed`** (a broken release) reverses that attempt.
- **Only these messages count**: the dispatcher sends `completed` only for a controller that exited 0 with `status: completed`.
- **Analytics** in Databricks are built from the three objects above, with the same rule: only `status.json` with `completed` counts.

## Match requests

The backend sends a message to the SQS match queue:

```json
{"match_id": "m-7f3a", "submissions": ["sub-123", "sub-456"], "ranked": true, "game": {"map": "arena-3"}}
```

- `submissions` are in slot order. `ranked` selects on-demand capacity (finals) over Spot (ladder).
- `match_id` is unique per match. A retried match keeps its `match_id` and gets a new `attempt`.
- Failed messages land in a dead-letter queue after a few attempts and raise an alarm.
- The queue is in the execution account; the backend may only send to it. Results come back on the backend's own results queue ([Results to the backend](#results-to-the-backend)).

## Versioning

- **Protocol:** any change to a message bumps `protocol` (in `hello`) and changes the controller, relay and starter kits together, in one PR in the platform repo. Starter kits in other languages check against JSON Schemas generated from the Rust types.
- **`Game` trait:** semantic versioning. Additive changes are minor versions, breaking changes are major. Changes are agreed between the platform and the game's maintainers.
- **Game logic:** every rules change bumps the game crate's version, which is recorded in results and replays.
