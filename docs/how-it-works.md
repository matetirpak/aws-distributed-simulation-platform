# How it works

For readers who don't know Rust. Part 1 explains the tools and the pieces. Part 2 follows one match from the command you type to the printed result, step by step, with a link to the exact code for every step.

Code references look like [`relay/src/main.rs:76`](../platform/crates/relay/src/main.rs#L76): file and line, clickable on GitHub and in VS Code. If a line has moved since this was written, search the file for the function name given next to it.

---

## Part 1: tools and pieces

### Cargo, compared to tools you know

| Rust / Cargo | Closest thing you know |
|---|---|
| `cargo` | One CLI that downloads libraries, compiles, tests and lints, like `terraform` and pip in one |
| `Cargo.toml` | Project file: name, dependencies (like `pyproject.toml`) |
| `Cargo.lock` | Exact versions and checksums of every library (like `.terraform.lock.hcl`) |
| `rust-toolchain.toml` | Which Rust version to use (like `.terraform-version`) |
| crate | One package, like one Terraform module |
| `cargo build` | Compile everything; programs land in `target/debug/` (`target/release/` with `--release`) |
| `cargo run -p local-runner -- coin ok ok` | Build, then run the program from the crate `local-runner`; everything after `--` is passed to that program |
| `cargo test` | Build and run all tests |
| `cargo fmt` / `cargo clippy` | `terraform fmt` / `tflint` |

**Rust is compiled.** `cargo build` turns the source code into a single executable file per program, which runs without Rust installed. The libraries a program uses are copied into that file, so nothing else is needed at run time. That's why the controller image can hold one 5 MB file and nothing else.

### The six crates

A crate with a `src/main.rs` is a **program** (an executable). A crate with a `src/lib.rs` is a **library**: code that programs use, like a Terraform module that root modules call. A crate's `Cargo.toml` lists what it uses; `controller.workspace = true` means "the crate `controller` from this repo".

| Crate | Kind | What it is | Main file |
|---|---|---|---|
| `protocol` | library | The message formats every side agrees on: `hello`, `init`, `turn`, `action`, `end`, the match spec, the result | [`protocol/src/lib.rs`](../platform/crates/protocol/src/lib.rs) |
| `controller` | library | The game server's logic: accept bots, check tokens, send turns, time the answers, write results | [`controller/src/lib.rs`](../platform/crates/controller/src/lib.rs) |
| `games` | library | Two toy games (coin, Nim) that stand in for the real game, plus broken games for tests | [`games/src/lib.rs`](../platform/crates/games/src/lib.rs) |
| `mock-controller` | **program** | The game server you actually run: picks a toy game and starts the controller logic | [`mock-controller/src/main.rs`](../platform/crates/mock-controller/src/main.rs) |
| `relay` | **program** | Runs in each bot container: connects to the game server, starts the bot, carries messages between them | [`relay/src/main.rs`](../platform/crates/relay/src/main.rs) |
| `local-runner` | **program** | Runs on your laptop and does the dispatcher's job: creates a match, starts the containers, shows the result | [`local-runner/src/main.rs`](../platform/crates/local-runner/src/main.rs) |

```
protocol ◀── controller ◀── games ◀── mock-controller        arrows: "is used by"
   ▲              ▲            ▲
   ├── relay      └────────────┴──── local-runner
```

`bots/python/bot.py` is plain Python: the stand-in for a contestant's bot.

### Who runs where during a match

```
your laptop                        Docker
───────────                        ──────────────────────────────────────────────────────────────
local-runner ── docker compose ──▶ container "controller"      network match0   container "bot0"
                                     /mock-controller  ◀──── TCP 9000 ───────── /relay
                                     writes /out/...                               │ pipes (stdin/stdout/stderr)
                                                                                   ▼
                                                                                 python3 /bot/bot.py
                                                    ◀──── TCP 9000 ─ network match1 ─ container "bot1" (same)
```

Three processes talk in a match: `mock-controller`, two `relay`s, and the two `python3` processes the relays start. Between the controller and a relay is a **TCP connection**. Between a relay and its bot are **pipes**, not a network.

---

## Part 2: one match, start to finish

Command: `cargo run -p local-runner -- coin ok ok`

### Step 1: Cargo builds and starts `local-runner`

Cargo compiles the crates `local-runner` depends on (`protocol`, `controller`, `games`) into one program, `target/debug/local-runner`, and starts it with the arguments `coin ok ok`.

- [`local-runner/src/main.rs:34`](../platform/crates/local-runner/src/main.rs#L34) `main` checks the arguments (game, mode of bot 0, mode of bot 1) and calls `run`.

### Step 2: `local-runner` creates the match

[`local-runner/src/main.rs:75`](../platform/crates/local-runner/src/main.rs#L75) `run`:

1. **Repo root:** `CARGO_MANIFEST_DIR` is the path of `crates/local-runner`, baked in at compile time, so `../..` is the repo root ([line 76](../platform/crates/local-runner/src/main.rs#L76)).
2. **Match ID and tokens:** a match ID like `local-1791912521-06268f`, and two tokens of 16 random bytes each, read from `/dev/urandom` ([lines 80–81](../platform/crates/local-runner/src/main.rs#L80); `random` at [line 303](../platform/crates/local-runner/src/main.rs#L303)).
3. **The match spec** ([lines 82–98](../platform/crates/local-runner/src/main.rs#L82)), the JSON the dispatcher will send on AWS: match ID, a random seed, `players` (slot 0 with token 0, slot 1 with token 1), and limits (defaults, except `connect_ms` = 30 s locally).
4. **The output folder** `out/<match_id>/` ([line 99](../platform/crates/local-runner/src/main.rs#L99)). Its owner's user and group IDs are noted, so the controller's files belong to you.
5. **Environment variables for compose** ([lines 106–124](../platform/crates/local-runner/src/main.rs#L106)):
   - `MATCH_SPEC` (the spec as JSON)
   - `MOCK_GAME=coin`
   - `TOKEN0`, `TOKEN1`
   - `MODE0=ok`, `MODE1=ok`
   - `OUT_DIR`, `HOST_UID`, `HOST_GID`

   They're passed to the `docker` process's environment, never on the command line, because the spec contains the tokens.

### Step 3: Docker Compose builds the images and starts three containers

`local-runner` runs `docker compose --project-name pg-<id> up --build --detach --quiet-build --quiet-pull controller bot0 bot1` from the repo root ([lines 139–148](../platform/crates/local-runner/src/main.rs#L139); the command is assembled in `Compose::command`, [line 252](../platform/crates/local-runner/src/main.rs#L252)). Compose reads [`compose.yaml`](../platform/compose.yaml) and replaces every `${...}` with the variables from step 2. (With `--controller-image`, see [below](#running-a-real-games-controller-image), it builds only the bot image and starts with `--no-build`.)

**Building** ([`Dockerfile`](../platform/Dockerfile), two images from one file):
1. Stage `build` ([lines 6–14](../platform/Dockerfile#L6)) starts from the official Rust image, copies the repo in, and runs `cargo build --release --locked -p mock-controller -p relay`. The results are two executable files, copied to `/dist/`.
2. Stage `controller` ([lines 17–25](../platform/Dockerfile#L17)) starts from `distroless/static`, an image with no shell and no package manager. It copies in only `/mock-controller` and sets `PORT=9000`, `OUT_DIR=/out`, and `ENTRYPOINT ["/mock-controller"]`: the command the container runs.
3. Stage `bot-python` ([lines 28–34](../platform/Dockerfile#L28)) starts from a Python image. It copies in `/relay` and `bots/python/bot.py` (as `/bot/bot.py`), creates user 10001, and sets `ENTRYPOINT ["/relay", "--", "python3", "/bot/bot.py"]`.

The second time, Docker reuses its cache and building takes seconds.

**Networks** ([`compose.yaml:45–47`](../platform/compose.yaml#L45)):
- `match0` connects the controller and bot0, and `match1` the controller and bot1.
- Both are `internal: true`: no route to the internet, and the two bots share no network.
- On a compose network, Docker's built-in DNS resolves a **service name** to that container's IP address, so `controller` means "the controller container".

**Containers** (`compose.yaml`):

| Container | Command it runs | Environment | Other settings |
|---|---|---|---|
| `controller` ([line 7](../platform/compose.yaml#L7)) | `/mock-controller` | `MATCH_SPEC`, `MOCK_GAME=coin`, plus `PORT=9000`, `OUT_DIR=/out` and `GAME_VERSION=mock` from the Dockerfile | Your folder `out/<match_id>` mounted at `/out`; runs as your user; read-only filesystem |
| `bot0` ([line 21](../platform/compose.yaml#L21)) | `/relay -- python3 /bot/bot.py` | `CONTROLLER_ADDR=controller:9000`, `BOT_TOKEN=<token 0>`, `BOT_MODE=ok` | Read-only filesystem, 512 MB memory, 64 processes, 1 CPU, no Linux capabilities |
| `bot1` ([line 37](../platform/compose.yaml#L37)) | same | same, with token 1 and `MODE1` | same, on network `match1` |

All three start at the same moment, with no fixed order. A relay that starts before the controller listens just retries (step 5).

### Step 4: the controller starts and listens

Inside the `controller` container, `/mock-controller` starts:

1. **Pick the game:** [`mock-controller/src/main.rs:8`](../platform/crates/mock-controller/src/main.rs#L8) reads `MOCK_GAME=coin` and calls `controller::run::<Coin>()`. `<Coin>` means "run the controller with the rules of the coin game".
2. [`controller/src/lib.rs:44`](../platform/crates/controller/src/lib.rs#L44) `run`:
   - **Logging:** JSON lines on stdout ([`init_logging`, line 116](../platform/crates/controller/src/lib.rs#L116)).
   - **Panic hook:** a crash inside game code is reported once by the caller, instead of being printed on every turn ([`panics.rs`](../platform/crates/controller/src/panics.rs)).
   - **Configuration:** `Config::from_env` ([line 92](../platform/crates/controller/src/lib.rs#L92)) reads `MATCH_SPEC`, `PORT`, `OUT_DIR` and `GAME_VERSION`. It checks the spec strictly with [`MatchSpec::parse` (`protocol/src/spec.rs:117`)](../platform/crates/protocol/src/spec.rs#L117). A bad spec ends the program here with exit code 2.
   - **Game setup:** `Coin::setup` ([`games/src/coin.rs:87`](../platform/crates/games/src/coin.rs#L87)) builds the board and places 10 coins, using a random generator seeded with the spec's seed. The same seed always gives the same board.
   - **Async runtime:** starts *tokio* ([lines 70–81](../platform/crates/controller/src/lib.rs#L70)), the library that lets one program wait on many network connections at once. It runs small units of work called *tasks* on 2 operating-system threads.
3. [`controller/src/play.rs:20`](../platform/crates/controller/src/play.rs#L20) `run_match`:
   - **Listen:** opens a TCP listener on `0.0.0.0:9000` ([line 37](../platform/crates/controller/src/play.rs#L37)) and logs `{"message":"listening","port":9000,...}` ([line 55](../platform/crates/controller/src/play.rs#L55)).
   - **Play, unless stopped:** runs `play(...)` and waits for `SIGTERM` at the same time ([`select!`, lines 58–73](../platform/crates/controller/src/play.rs#L58)). Whichever comes first wins: the match finishing, or Docker/ECS stopping the container.
4. [`play.rs:113`](../platform/crates/controller/src/play.rs#L113) `play` first calls [`lobby::connect_players` (`lobby.rs:34`)](../platform/crates/controller/src/lobby.rs#L34) and waits there until both bots have connected (step 6).

### Step 5: each relay connects to the controller, then starts its bot

Inside `bot0`, Docker runs `/relay -- python3 /bot/bot.py`. [`relay/src/main.rs:50`](../platform/crates/relay/src/main.rs#L50) `main`:

1. **Read the bot command:** everything after `--` is the command to start the bot: `["python3", "/bot/bot.py"]` ([lines 51–57](../platform/crates/relay/src/main.rs#L51)).
2. **Read the address and token:** `CONTROLLER_ADDR=controller:9000` and `BOT_TOKEN` from the environment ([line 58](../platform/crates/relay/src/main.rs#L58)).
3. **Connect:** [`connect` (line 131)](../platform/crates/relay/src/main.rs#L131) opens a TCP connection to `controller:9000`. Docker's DNS turns `controller` into the controller container's IP on `match0`. If the controller isn't listening yet, it retries every 250 ms for up to 120 s.
4. **Authenticate:** [lines 67–75](../platform/crates/relay/src/main.rs#L67) send the first line, `{"type":"hello","token":"<token 0>","protocol":1}`.
5. **Start the bot. This is how the relay and the bot are linked** ([lines 76–88](../platform/crates/relay/src/main.rs#L76)):

   ```rust
   Command::new("python3").args(["/bot/bot.py"])   // start this program as a child process
       .env_remove("CONTROLLER_ADDR")              // the bot never sees the address...
       .env_remove("BOT_TOKEN")                    // ...or the token
       .env("PYTHONUNBUFFERED", "1")               // Python sends each line immediately
       .stdin(Stdio::piped())                      // the relay writes what the bot reads
       .stdout(Stdio::piped())                     // the relay reads what the bot prints
       .stderr(Stdio::piped())                     // the relay reads the bot's debug output
       .spawn()
   ```

   - **What a pipe is:** a one-way channel the operating system provides between two processes. It's the same thing as `|` in a shell, but held open for the whole match.
   - **What the bot sees:** `python3 /bot/bot.py` runs as a normal child process of the relay. Its `sys.stdin` is the read end of a pipe the relay writes into, its `print()` goes into a pipe the relay reads, and the same for stderr.
   - **No network for the bot:** the bot never opens a connection; only the relay holds the TCP socket. The bot cannot tell whether it's talking to a relay or to a person typing in a terminal.
6. **Four threads**, each blocking on one read, so nothing ever spins on the CPU ([lines 101–116](../platform/crates/relay/src/main.rs#L101)):

   | Thread | Function | Reads from | Writes to |
   |---|---|---|---|
   | 1 | [`from_controller` (line 153)](../platform/crates/relay/src/main.rs#L153) | TCP socket (controller) | an internal queue for thread 2, plus the list of open requests |
   | 2 | [`write_to_bot` (line 193)](../platform/crates/relay/src/main.rs#L193) | that internal queue | the bot's stdin pipe |
   | 3 | [`to_controller` (line 203)](../platform/crates/relay/src/main.rs#L203) | the bot's stdout pipe | TCP socket (controller) |
   | 4 | [`capture_stderr` (line 260)](../platform/crates/relay/src/main.rs#L260) | the bot's stderr pipe | a 64 KiB buffer (debug info) |

   The main thread then waits ([line 120](../platform/crates/relay/src/main.rs#L120)) until either the controller closes the connection (thread 1 ends) or the bot's stdout closes (thread 3 ends).

`bot1` does exactly the same, with token 1, on network `match1`.

### Step 6: the controller accepts both relays (the lobby)

In the controller, [`lobby.rs:34`](../platform/crates/controller/src/lobby.rs#L34) `connect_players`:

1. **The token table:** builds `token → slot` from the spec ([lines 40–45](../platform/crates/controller/src/lobby.rs#L40)) and starts [`accept_loop` (line 66)](../platform/crates/controller/src/lobby.rs#L66) as a task.
2. **Each new connection** gets its own [`handshake` (line 96)](../platform/crates/controller/src/lobby.rs#L96) task. At most 16 can be waiting at once; more are closed at once.
3. **`handshake` checks the first line** ([lines 107–140](../platform/crates/controller/src/lobby.rs#L107)):
   - It reads one line, waiting at most 2 s.
   - The line must be a `hello` with `"protocol": 1`.
   - It **removes** the token from the table ([line 124](../platform/crates/controller/src/lobby.rs#L124)): that's what makes tokens single-use, and the token decides the slot.
   - If anything is wrong, it logs the reason (never the token) and closes the connection.
4. **Back in `connect_players`**, each authenticated connection is handed to its player with [`Player::attach` (`player.rs:90`)](../platform/crates/controller/src/player.rs#L90). `attach` starts a [`read_loop` (`player.rs:257`)](../platform/crates/controller/src/player.rs#L257) task for that connection.
5. **Every line from a bot goes through `read_loop`** from now on:
   - **Timestamp:** it notes the arrival time right when the line is complete ([line 269](../platform/crates/controller/src/player.rs#L269)).
   - **Parse:** it turns the line into a message ([lines 270–280](../platform/crates/controller/src/player.rs#L270)). A line over 64 KiB means `invalid_output` and the connection is closed. Unreadable JSON is marked "malformed".
   - **Queue:** it puts the result into that player's inbox, a queue the match loop reads.

   Taking the timestamp here, not in the match loop, means a busy match loop never makes a bot look late.
6. **When both slots are connected**, the listener closes ([line 59](../platform/crates/controller/src/lobby.rs#L59)). A slot nobody connected for within `connect_ms` gets `never_connected` ([lines 61–63](../platform/crates/controller/src/lobby.rs#L61)).

### Step 7: `init` and `ready`

Back in [`play` (`play.rs:113`)](../platform/crates/controller/src/play.rs#L113):

1. **Build the messages:** the game's static info goes into one `init` per player ([lines 125–135](../platform/crates/controller/src/play.rs#L125)), and they're handed to [`ask` (line 257)](../platform/crates/controller/src/play.rs#L257).
2. **`ask` sends to everyone at once:** it encodes each message as one JSON line and calls [`send_all` (`player.rs:214`)](../platform/crates/controller/src/player.rs#L214).
   - It writes to both connections at the same time, so a stuck bot can't delay the other one.
   - Each write may take at most 5 s, or that bot is `disconnected`.
   - For each bot it notes when sending began and when it finished.
3. **The relay passes it on:** relay thread 1 reads the line, sees `"type":"init"` ([line 176](../platform/crates/relay/src/main.rs#L176)), adds `Init` to its list of open requests, and passes the unchanged line to thread 2, which writes it into the bot's stdin. The bot receives, for example:
   ```json
   {"type":"init","slot":0,"players":2,"config":{"board":["........","..#..#..",...],"height":8,"width":8},"budget_ms":5000}
   ```
4. **The bot answers:** [`bots/python/bot.py:92`](../platform/bots/python/bot.py#L92) `main` reads it with `sys.stdin.readline()` and prints `ready` (`answer("ready")`, [line 109](../platform/bots/python/bot.py#L109)).
5. **The relay translates it:** relay thread 3 reads `ready` from the bot's stdout. It takes the oldest open request (`Init`, [line 228](../platform/crates/relay/src/main.rs#L228)) and sends `{"type":"ready"}` to the controller ([line 232](../platform/crates/relay/src/main.rs#L232)). Any line counts as ready.
6. **The controller checks the timing:** `read_loop` timestamps and queues it. [`await_reply` (`player.rs:161`)](../platform/crates/controller/src/player.rs#L161), the function that waits for one bot's answer, takes it from the inbox:
   - The deadline is "send finished + `init_ms` + `grace_ms`" ([line 162](../platform/crates/controller/src/player.rs#L162)).
   - A line that arrived before sending began is stale and skipped ([line 192](../platform/crates/controller/src/player.rs#L192)).
   - A `ready` is accepted ([lines 194–196](../platform/crates/controller/src/player.rs#L194)).

   No answer in time means `timeout` ([`play.rs:138`](../platform/crates/controller/src/play.rs#L138)).

### Step 8: the turn loop

[`play.rs:147`](../platform/crates/controller/src/play.rs#L147), repeated for turn 1, 2, 3 …:

1. **Stop or continue:** stop if the game is over, fewer than 2 players are still active, or the total match time is used up ([lines 148–157](../platform/crates/controller/src/play.rs#L148)).
2. **Who moves:** `players_to_move()` returns both slots for the coin game, one for Nim ([line 159](../platform/crates/controller/src/play.rs#L159)). For each mover the game builds its view of the state ([lines 161–163](../platform/crates/controller/src/play.rs#L161)).
3. **Send the turn:** `ask` sends one `turn` line per mover, the same way as `init` ([lines 169–176](../platform/crates/controller/src/play.rs#L169)):
   ```json
   {"type":"turn","turn":1,"state":{"coins":[[1,0],[2,7],...],"positions":[[0,0],[7,7]],"scores":[0,0]},"budget_ms":2000}
   ```
4. **The relay notes the turn number:** relay thread 1 sees `"type":"turn","turn":1`, adds `Turn(1)` to the open requests **before** passing the line on ([line 177](../platform/crates/relay/src/main.rs#L177)), and the bot reads it from stdin.
5. **The bot answers:** `bot.py` computes a move and prints one line: `{"move": "E"}` ([line 130](../platform/bots/python/bot.py#L130)). `answer` flushes stdout immediately ([line 38](../platform/bots/python/bot.py#L38)).
6. **The relay wraps the answer:** relay thread 3 reads that line, takes the oldest open request (`Turn(1)`), and sends `{"type":"action","turn":1,"action":{"move":"E"}}` ([lines 233–238](../platform/crates/relay/src/main.rs#L233)).
   - If the line isn't JSON, it sends `{"type":"action","turn":1,"invalid":true}` instead ([lines 239–243](../platform/crates/relay/src/main.rs#L239)).
   - Because answers are matched to requests in order, a late answer still carries its own turn number.
7. **The controller checks it:** `read_loop` timestamps the line, and `await_reply` accepts it only if:
   - it arrived before the deadline (send finished + `turn_ms` + `grace_ms`)
   - its `turn` matches the current turn ([lines 197–203](../platform/crates/controller/src/player.rs#L197)); an answer for an older turn is ignored ([line 206](../platform/crates/controller/src/player.rs#L206))
8. **The game judges the action** ([`play.rs:181–221`](../platform/crates/controller/src/play.rs#L181)):
   - **No answer:** a timeout. After `max_timeouts` timeouts the bot forfeits with `timeout`.
   - **An answer:** the game's `validate` decides whether the move is legal ([line 200](../platform/crates/controller/src/play.rs#L200); for coin: [`coin.rs:147`](../platform/crates/games/src/coin.rs#L147)).
   - **Invalid** (an illegal move, `invalid:true`, malformed, or `validate` crashing): one more invalid action ([`count_invalid`, line 287](../platform/crates/controller/src/play.rs#L287)). After `max_invalid` of them the bot forfeits with `invalid_output`.
9. **Apply and record:** the game applies all moves ([line 222](../platform/crates/controller/src/play.rs#L222); for coin: [`coin.rs:151`](../platform/crates/games/src/coin.rs#L151)), and the turn is recorded in the replay: who moved, the valid actions, the times ([lines 223–228](../platform/crates/controller/src/play.rs#L223)).

### Step 9: the end of the match

1. **Scores, ranks, `end`:** after the loop, the game returns the scores, and [`compute_ranks` (`play.rs:315`)](../platform/crates/controller/src/play.rs#L315) turns them into ranks: forfeited players rank last, equal scores share a rank. Each still-connected bot gets `{"type":"end","rank":1,"score":5}` ([lines 231–252](../platform/crates/controller/src/play.rs#L231)).
2. **Close the connections:** back in `run_match`, the controller closes every connection ([lines 75–77](../platform/crates/controller/src/play.rs#L75)).
3. **The relay ends the bot's input:** relay thread 1 passes `end` to the bot and then stops forwarding ([line 184](../platform/crates/relay/src/main.rs#L184)). Thread 2 writes `end` into the bot's stdin and then closes it. When the controller's close arrives, thread 1 ends and reports "controller closed" ([line 188](../platform/crates/relay/src/main.rs#L188)).
4. **The bot exits:** `bot.py` reads `end`, writes `end: rank 1, score 5` to stderr, and returns ([lines 112–114](../platform/bots/python/bot.py#L112)), so the Python process exits.
5. **The relay reports and exits:**
   - It waits up to 2 s for the bot to exit, and kills it otherwise ([`wait_or_kill`, line 275](../platform/crates/relay/src/main.rs#L275)).
   - It writes one JSON line to its own stderr: the bot's exit code and its captured stderr ([`report`, line 304](../platform/crates/relay/src/main.rs#L304)).
   - It exits, and so the `bot0` container stops.
6. **The controller writes the output:** it builds the result and calls [`output::write` (`output.rs:13`)](../platform/crates/controller/src/output.rs#L13):
   - first `replay.json.gz` (gzip-compressed JSON of every turn)
   - then `result.json`

   Each is written to a temporary name and then renamed, so a file is either complete or absent. `/out` inside the container is your `out/<match_id>/` folder.
7. **The controller exits:** it logs `match finished` and exits with code 0 for a completed match, so the `controller` container stops ([lines 104–109](../platform/crates/controller/src/play.rs#L104)).

### Step 10: `local-runner` collects and shows the result

Back on your laptop, in `run`:

1. **Wait:** `docker compose wait controller` blocks until the controller container stops and returns its exit code ([lines 150–157](../platform/crates/local-runner/src/main.rs#L150)). Then it waits for the bot containers too ([lines 159–163](../platform/crates/local-runner/src/main.rs#L159)).
2. **Collect the bots' reports:** `docker compose logs bot0` (and bot1) holds the relay's report line, and [`print_relay_report` (line 223)](../platform/crates/local-runner/src/main.rs#L223) prints its exit code and the first lines of the bot's stderr.
3. **Clean up:** `drop(compose)` runs `docker compose down`, which removes the containers and networks ([line 173](../platform/crates/local-runner/src/main.rs#L173); `Drop` at [line 284](../platform/crates/local-runner/src/main.rs#L284)). This also runs if anything above failed.
4. **Show the result:** it reads `out/<match_id>/result.json` and prints the table ([lines 183–202](../platform/crates/local-runner/src/main.rs#L183)).
5. **Check the replay:** it reads `replay.json.gz` and re-plays every recorded action through the coin rules with [`controller::verify` (`verify.rs:8`)](../platform/crates/controller/src/verify.rs#L8), comparing the final scores. `replay re-simulated: OK` means the match is reproducible from its replay ([lines 204–219](../platform/crates/local-runner/src/main.rs#L204)).

---

## What changes in the other scenarios

The same flow, with one step behaving differently:

| Scenario | What happens, and where |
|---|---|
| `slow` | Every 5th turn the bot sleeps past the budget ([`bot.py:119`](../platform/bots/python/bot.py#L119)). `await_reply` hits the deadline ([`player.rs:169`](../platform/crates/controller/src/player.rs#L169)); after 3 timeouts the bot forfeits (`play.rs:186–191`) |
| `crash` | The bot exits on turn 3 ([`bot.py:122`](../platform/bots/python/bot.py#L122)), so its stdout closes. The relay closes the TCP connection ([`relay/main.rs:120–121`](../platform/crates/relay/src/main.rs#L120)). The controller's `read_loop` sees the close ([`player.rs:279`](../platform/crates/controller/src/player.rs#L279)) → `disconnected` |
| `garbage` | The bot prints `not json` or `{"move":"TELEPORT"}`. The relay sends `invalid:true`, or the game's `validate` rejects the move. Each counts as invalid; after 10 → `invalid_output` |
| `huge` | The bot prints a 70 KB line. The relay passes it on past the limit ([`relay/main.rs:218–222`](../platform/crates/relay/src/main.rs#L218)). `read_loop` sees a line over 64 KiB ([`player.rs:278`](../platform/crates/controller/src/player.rs#L278)) → `invalid_output` and the connection is closed |
| `silent`, `noflush` | No `ready` reaches the controller: silent never prints, and noflush's line stays in Python's own buffer ([`bot.py:28`](../platform/bots/python/bot.py#L28)). The init deadline passes → `timeout` ([`play.rs:138`](../platform/crates/controller/src/play.rs#L138)) |
| `absent` | `local-runner` doesn't start `bot1` ([`local-runner/main.rs:130–133`](../platform/crates/local-runner/src/main.rs#L130)). The lobby waits `connect_ms` (30 s) → `never_connected` ([`lobby.rs:61–63`](../platform/crates/controller/src/lobby.rs#L61)) |
| `probe` | At `init`, the bot tries the internet, the metadata service, the credentials endpoint, public DNS and the other bot ([`bot.py`, `probe`](../platform/bots/python/bot.py)). With the `internal` networks nothing answers, so it plays normally → `ok` |

## Running a real game's controller image

`cargo run -p local-runner -- --controller-image <image> ok ok` plays the same match with another controller image, for example one built by a game's release workflow. That's its smoke match before the image is pushed.

| What changes | Where |
|---|---|
| The argument parser takes the image and leaves out the game name | [`local-runner/src/main.rs:34`](../platform/crates/local-runner/src/main.rs#L34) `main` |
| `CONTROLLER_IMAGE` replaces the controller's `image:` in compose | [`compose.yaml:11`](../platform/compose.yaml#L11) |
| Only the bot image is built; the controller image is used as given (`up --no-build`) | [`local-runner/src/main.rs:139–143`](../platform/crates/local-runner/src/main.rs#L139) |
| Our bot doesn't know that game, so it answers every turn with `{}` | [`bots/python/bot.py`](../platform/bots/python/bot.py), the `else` branch at the end of `main` |
| The replay isn't re-simulated, because we don't have that game's rules. The game re-simulates its replays in its own tests | [`local-runner/src/main.rs:206–209`](../platform/crates/local-runner/src/main.rs#L206) |
| **Pass** = the controller exits 0 and `result.json` says `completed`. The bots' outcomes don't matter | [`local-runner/src/main.rs:219`](../platform/crates/local-runner/src/main.rs#L219) |

## The dispatcher

`local-runner` plays one match and stops. The **dispatcher** is the service that runs all the time on AWS: it takes match requests from a queue, plays them, decides what each one means, and reports to the backend. It's Python (`dispatcher/`). It shares no code with the Rust parts, only JSON: the match spec it generates, the `result.json` it reads, and the queue messages.

```
backend ──request──▶ match queue ──▶ dispatcher ──▶ controller task, then bot tasks
                                         ▲    │
                      every few seconds: │    ▼ when the controller stops: exit code + result.json
                      "how are my tasks?"│    decide ──▶ results message ──▶ backend
                                         └────┘         (void? queue attempt + 1)
```

The logic is one function, `tick()`, run every few seconds ([`core.py`](../platform/dispatcher/dispatcher/core.py)). It talks to the world only through the interfaces in [`ports.py`](../platform/dispatcher/dispatcher/ports.py): `Queue`, `Tasks`, `Store`, `Results` and `Control`. [`local.py`](../platform/dispatcher/dispatcher/local.py) implements them with docker compose and files; the AWS versions (ECS, SQS, S3) come later and change nothing in `core.py`.

### One match through the dispatcher

| Step | What happens | Code |
|---|---|---|
| 1 | The request arrives: `match_id`, two submissions, `ranked`, `game`. A retry is the same request with `attempt` + 1 | `MatchRequest.parse` in [`model.py`](../platform/dispatcher/dispatcher/model.py) |
| 2 | Two fresh 128-bit tokens and a 32-bit seed become the match spec, with only the keys the controller accepts | `Secrets.new`, `build_spec` |
| 3 | The controller task starts first | `_start` → `_request_controller` |
| 4 | Once it's running and has an address, both bots start with that address and their tokens. Ladder bots start on Spot; one still not running after 60 s is relaunched on-demand | `_step` → `_start_bots` |
| 5 | Every tick: ask about all tasks at once, then move each match on | `_advance`, `_step` |
| 6 | The controller stops: its exit code and `result.json` decide | `_finish` |
| 7 | Report: results message, `status.json`, stop the leftovers, queue the retry if any, delete the request **last** | `_report` |
| 8 | Keep the requests of running matches invisible to the other dispatcher, by extending them before half their visibility timeout is gone | `_heartbeat` |

### What it decides

| Situation | Decision |
|---|---|
| Exit 0, `result.json` says `completed` | **completed**: the result goes to the backend as is |
| A bot never runs within the deadline (5 min on AWS, 15–20 s locally) | **void** `bot_never_started` |
| A task stopped by the infrastructure (Spot, host failure), even mid-match | **void** `infrastructure`, at once: an infrastructure failure must never count as a loss |
| A bot stopped by an operator or the reaper | **void** `stopped` |
| A bot that crashes **by itself** | No void: the controller recorded `disconnected`, and that bot loses |
| `aborted` (the controller got SIGTERM) | **void** `aborted` |
| `controller_error`, or no readable `result.json` | **void** `controller_error`; it counts toward the circuit breaker |
| Exit 2: the controller rejected its spec | **void** `bad_config`, never retried, and an `ALERT` in the log: our bug |
| The controller runs past `connect_ms + total_ms` + 2 min | stop it, **void** `stuck` |

A void is retried as attempt 2, then 3. The third void is final (`retry: false`, an `ALERT` in the log).

### When things go wrong in the dispatcher itself

- **A call fails** (docker hiccup, AWS throttling): it's logged, and the next tick tries again. One failing match never stops the others (`_guard`).
- **A dispatcher task crashes** mid-match: its request reappears in the queue after the visibility timeout, and the other dispatcher picks it up.
  - If all three tasks exist, it **adopts** them: it watches them and starts nothing new, because the controller expects the crashed dispatcher's tokens.
  - If only some exist, those tokens are lost, so the attempt is voided (`dispatcher_restarted`) and retried.
- **A request arrives twice** (queues deliver at least once): `status.json` marks a reported attempt, so it's never played twice.
- **Many `controller_error`s** (a broken release): when they reach 5% of the attempts finished in the last 10 minutes (at least 20), the [circuit breaker](../platform/dispatcher/dispatcher/breaker.py) trips. The dispatcher takes no new matches until an operator resets it; matches already running finish.

### Locally vs. on AWS

| Port | Locally ([`local.py`](../platform/dispatcher/dispatcher/local.py)) | On AWS (later) |
|---|---|---|
| `Queue` | In memory, with visibility timeouts and delays | The SQS match queue |
| `Tasks` | One compose project per attempt (`pgd-<match>-<attempt>`), from `compose.yaml` | ECS `RunTask` / `DescribeTasks` / `StopTask`, idempotent client tokens |
| `Store` | `out/dispatcher/<match>/<attempt>/` | The replay bucket |
| `Results` | `out/dispatcher/results.jsonl` | The backend's results queue |
| `Control` | `touch out/dispatcher/reset-breaker` | An SSM parameter |

The tests: [`tests/test_core.py`](../platform/dispatcher/tests/test_core.py) plays each situation above against in-memory fakes ([`tests/fakes.py`](../platform/dispatcher/tests/fakes.py)), tick by tick. [`tests/test_contract.py`](../platform/dispatcher/tests/test_contract.py) runs a spec built by the dispatcher through the real controller binary and parses the `result.json` it writes. [`tests/test_local_e2e.py`](../platform/dispatcher/tests/test_local_e2e.py) plays three real matches with Docker.

## How the tests cut this flow short

- **Protocol tests** ([`mock-controller/tests/protocol.rs`](../platform/crates/mock-controller/tests/protocol.rs)) start the real `mock-controller` program on your machine, without Docker. The test itself plays both bots: it opens TCP connections and types the protocol lines that the relay and `bot.py` would otherwise produce. That way each step above can be broken on purpose (a late answer, a wrong token, a huge line) at exactly the right moment.
- **Relay tests** ([`relay/tests/relay.rs`](../platform/crates/relay/tests/relay.rs)) start the real `relay` program. The test plays the controller (it listens on a port and sends `init`, `turn`, `end`), and the bot is a few lines of Python.

## Enough Rust syntax to read the code

| Code | Means |
|---|---|
| `fn name(a: T) -> R` | A function taking `a` of type `T`, returning `R` |
| `let x = ...;` / `let mut x` | A variable / a variable that can change |
| `struct Coin { ... }` | A data type with fields (like a Python class with only attributes) |
| `enum Pending { Init, Turn(u32) }` | A type that is exactly one of several variants, here "Init" or "Turn with a number" |
| `trait Game { ... }` | An interface: functions a type must provide (like a Python abstract base class) |
| `impl Game for Coin { ... }` | "Here is how Coin provides those functions": the coin game's rules |
| `controller::run::<Coin>()` | Call `run` from the `controller` crate, with `Coin` filled in as the game |
| `match x { A => ..., B => ... }` | A switch statement that must cover every case |
| `Option<T>`: `Some(v)` / `None` | A value that may be missing (like `None` in Python) |
| `Result<T, E>`: `Ok(v)` / `Err(e)` | Success or failure; `?` after a call means "if it failed, return the error now" |
| `&x` / `&mut x` | Pass `x` without handing it over: read-only / allowed to change it |
| `async fn` / `.await` | A function that can wait for the network without blocking a thread (tokio) |
| `thread::spawn(...)` | Start an operating-system thread (the relay uses these) |
| `#[cfg(test)]` | "Only compile this when running tests": unit tests at the bottom of a file |
| `//` and `///` | Comments; `///` documents the item below it |
