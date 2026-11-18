//! Test helpers: the real controller binary, and scripted fake bots over real TCP on localhost.
//! No Docker, no AWS.

#![allow(dead_code)]

use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use protocol::{MatchResult, Replay};
use serde_json::{Value, json};

/// Short budgets keep the suite fast; waits use wide margins over them, so slow CI runners
/// don't cause flaky results.
pub fn fast_limits() -> Value {
    json!({"connect_ms": 5_000, "init_ms": 1_000, "turn_ms": 300, "grace_ms": 100, "max_turns": 20, "total_ms": 60_000})
}

const WAIT: Duration = Duration::from_secs(5);
static COUNTER: AtomicU32 = AtomicU32::new(0);

fn unique(prefix: &str) -> String {
    format!(
        "{prefix}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    )
}

/// Starts the controller on PORT=0: the kernel picks a free port while binding it, so tests
/// running in parallel can never collide. (Reserving a "free" port first and starting the
/// controller on it afterwards is racy: another test can grab the same port in between.)
fn spawn_controller(game: &str, spec: &str, out_dir: &PathBuf) -> Child {
    Command::new(env!("CARGO_BIN_EXE_mock-controller"))
        .env("MOCK_GAME", game)
        .env("MATCH_SPEC", spec)
        .env("PORT", "0")
        .env("OUT_DIR", out_dir)
        .env("GAME_VERSION", "test")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start mock-controller")
}

/// Reads a pipe to the end in a background thread, so a full pipe never blocks the child.
fn read_all(mut pipe: impl Read + Send + 'static, prefix: String) -> JoinHandle<String> {
    thread::spawn(move || {
        let mut text = prefix;
        let _ = pipe.read_to_string(&mut text);
        text
    })
}

/// Collects a child's stdout and stderr in background threads.
fn collect_logs(child: &mut Child) -> Vec<JoinHandle<String>> {
    vec![
        read_all(child.stdout.take().unwrap(), String::new()),
        read_all(child.stderr.take().unwrap(), String::new()),
    ]
}

/// Reads the controller's log until its `listening` line and returns the port it bound,
/// plus background readers for the rest of its output.
fn wait_until_listening(child: &mut Child) -> (u16, Vec<JoinHandle<String>>) {
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut seen = String::new();
    let port = loop {
        let mut line = String::new();
        if stdout.read_line(&mut line).unwrap_or(0) == 0 {
            panic!("the controller exited before listening; its log:\n{seen}");
        }
        seen.push_str(&line);
        let msg: Value = serde_json::from_str(&line).unwrap_or_default();
        if msg["message"] == "listening" {
            break msg["port"].as_u64().expect("port in the listening line") as u16;
        }
    };
    let logs = vec![
        read_all(stdout, seen),
        read_all(child.stderr.take().unwrap(), String::new()),
    ];
    (port, logs)
}

fn wait_for_exit(child: &mut Child, limit: Duration) -> i32 {
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status.code().expect("exited normally, not by a signal");
        }
        if started.elapsed() > limit {
            let _ = child.kill();
            panic!("the controller didn't exit within {limit:?}");
        }
        thread::sleep(Duration::from_millis(20));
    }
}

/// One controller process for one match, with fresh tokens, its own port and output directory.
pub struct Match {
    pub port: u16,
    pub tokens: Vec<String>,
    pub out_dir: PathBuf,
    child: Child,
    logs: Vec<JoinHandle<String>>,
}

pub struct Finished {
    pub code: i32,
    pub result: MatchResult,
    pub replay: Replay,
    pub logs: String,
}

impl Match {
    /// A 2-player match of `game`, with `limits` merged over [`fast_limits`].
    pub fn start(game: &str, limits: Value) -> Match {
        Match::start_with(game, json!({}), limits)
    }

    pub fn start_with(game: &str, game_config: Value, limits: Value) -> Match {
        let tokens: Vec<String> = (0..2).map(|_| unique("token")).collect();
        let mut all_limits = fast_limits();
        for (k, v) in limits.as_object().unwrap() {
            all_limits[k] = v.clone();
        }
        let spec = json!({
            "match_id": unique("test-match"), "attempt": 1, "seed": 7,
            "players": [{"slot": 0, "token": tokens[0]}, {"slot": 1, "token": tokens[1]}],
            "limits": all_limits, "game": game_config,
        });
        let out_dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(unique(game));
        let mut child = spawn_controller(game, &spec.to_string(), &out_dir);
        let (port, logs) = wait_until_listening(&mut child);
        Match {
            port,
            tokens,
            out_dir,
            child,
            logs,
        }
    }

    /// A bot for `slot` that has already sent its `hello`.
    pub fn bot(&self, slot: usize) -> Bot {
        let mut bot = self.connect();
        bot.hello(&self.tokens[slot]);
        bot
    }

    /// A connection that hasn't said anything yet.
    pub fn connect(&self) -> Bot {
        Bot::connect(self.port)
    }

    pub fn terminate(&self) {
        let status = Command::new("kill")
            .args(["-TERM", &self.child.id().to_string()])
            .status()
            .unwrap();
        assert!(status.success());
    }

    /// Waits for the controller to exit and reads its output.
    pub fn finish(mut self) -> Finished {
        let code = wait_for_exit(&mut self.child, Duration::from_secs(30));
        let logs = self
            .logs
            .drain(..)
            .map(|h| h.join().unwrap())
            .collect::<Vec<_>>()
            .join("");
        let result = std::fs::read_to_string(self.out_dir.join("result.json"))
            .unwrap_or_else(|e| panic!("no result.json ({e}); logs:\n{logs}"));
        Finished {
            code,
            result: serde_json::from_str(&result).unwrap(),
            replay: controller::read_replay(&self.out_dir.join("replay.json.gz")).unwrap(),
            logs,
        }
    }
}

impl Drop for Match {
    fn drop(&mut self) {
        if let Ok(None) = self.child.try_wait() {
            let _ = self.child.kill(); // never leave a controller running
            let _ = self.child.wait();
        }
    }
}

/// Runs the controller with `spec` and returns its exit code and logs, for specs that
/// must be rejected before it listens.
pub fn run_to_exit(game: &str, spec: &str) -> (i32, String) {
    let out_dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(unique("bad-spec"));
    let mut child = spawn_controller(game, spec, &out_dir);
    let logs = collect_logs(&mut child);
    let code = wait_for_exit(&mut child, Duration::from_secs(10));
    (code, logs.into_iter().map(|h| h.join().unwrap()).collect())
}

/// A fake bot that does exactly what the test tells it, step by step.
pub struct Bot {
    stream: TcpStream,
    reader: BufReader<TcpStream>,
}

impl Bot {
    fn connect(port: u16) -> Bot {
        let started = Instant::now();
        let stream = loop {
            // The controller may still be starting.
            match TcpStream::connect(("127.0.0.1", port)) {
                Ok(s) => break s,
                Err(_) if started.elapsed() < WAIT => thread::sleep(Duration::from_millis(20)),
                Err(e) => panic!("cannot connect to the controller: {e}"),
            }
        };
        let reader = BufReader::new(stream.try_clone().unwrap());
        Bot { stream, reader }
    }

    pub fn hello(&mut self, token: &str) {
        self.send(json!({"type": "hello", "token": token, "protocol": 1}));
    }

    pub fn send(&mut self, msg: Value) {
        self.send_raw(format!("{msg}\n").as_bytes());
    }

    pub fn send_raw(&mut self, data: &[u8]) {
        self.stream.write_all(data).unwrap();
    }

    pub fn action(&mut self, turn: u64, action: Value) {
        self.send(json!({"type": "action", "turn": turn, "action": action}));
    }

    /// Reads the next message and checks its type: protocol order is part of the contract.
    pub fn expect(&mut self, kind: &str) -> Value {
        self.stream.set_read_timeout(Some(WAIT)).unwrap();
        let mut line = String::new();
        let n = self
            .reader
            .read_line(&mut line)
            .unwrap_or_else(|e| panic!("waiting for {kind:?}: {e}"));
        assert!(n > 0, "connection closed while waiting for {kind:?}");
        let msg: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(msg["type"], kind, "expected {kind:?}, got {msg}");
        msg
    }

    /// Answers `init` with `ready`; `init` only comes once every bot is connected.
    pub fn join(&mut self) -> Value {
        let init = self.expect("init");
        self.send(json!({"type": "ready"}));
        init
    }

    pub fn is_closed_by_server(&mut self, within: Duration) -> bool {
        self.stream.set_read_timeout(Some(within)).unwrap();
        let mut line = String::new();
        match self.reader.read_line(&mut line) {
            Ok(0) => true,
            Err(e) => e.kind() == ErrorKind::ConnectionReset,
            Ok(_) => false,
        }
    }
}

pub type Strategy = fn(&Value) -> Value;

pub fn stay(_state: &Value) -> Value {
    json!({"move": "STAY"})
}

/// Nim: take one object from the first non-empty heap.
pub fn take_one(state: &Value) -> Value {
    let heaps = state["heaps"].as_array().unwrap();
    let heap = heaps.iter().position(|h| h.as_u64() > Some(0)).unwrap();
    json!({"heap": heap, "take": 1})
}

/// Lets a bot answer every turn immediately in a background thread, so the test can script
/// (and stall) the other bot. Returns the `end` message, or `None` if the connection closed.
pub fn autoplay(mut bot: Bot, strategy: Strategy) -> JoinHandle<Option<Value>> {
    thread::spawn(move || {
        bot.join();
        bot.stream
            .set_read_timeout(Some(Duration::from_secs(30)))
            .unwrap();
        let mut line = String::new();
        loop {
            line.clear();
            if bot.reader.read_line(&mut line).unwrap_or(0) == 0 {
                return None;
            }
            let msg: Value = serde_json::from_str(&line).unwrap();
            match msg["type"].as_str() {
                Some("turn") => bot.action(msg["turn"].as_u64().unwrap(), strategy(&msg["state"])),
                Some("end") => return Some(msg),
                _ => {}
            }
        }
    })
}
