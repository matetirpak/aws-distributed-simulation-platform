//! Relay tests: the real relay binary between a fake controller (this test) and small
//! Python bots. Needs `python3` on the PATH.

use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use protocol::MAX_LINE;
use serde_json::{Value, json};

const WAIT: Duration = Duration::from_secs(5);

/// Answers each turn with what it was sent, and which secret variables it can see.
const ECHO_BOT: &str = r#"
import json, os, sys
sys.stdin.readline()
print("ready", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg["type"] == "turn":
        seen = sorted(k for k in ("BOT_TOKEN", "CONTROLLER_ADDR") if k in os.environ)
        print(json.dumps({"echo": msg["state"], "env": seen}), flush=True)
"#;

struct Harness {
    relay: Child,
    conn: TcpStream,
    reader: BufReader<TcpStream>,
    // Read in the background: the relay's final report can be larger than a pipe buffer.
    stderr: thread::JoinHandle<String>,
}

impl Harness {
    /// Starts the relay with `python3 -c <bot>` and accepts its connection.
    fn start(bot: &str) -> Harness {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut relay = Command::new(env!("CARGO_BIN_EXE_relay"))
            .args(["--", "python3", "-c", bot])
            .env(
                "CONTROLLER_ADDR",
                listener.local_addr().unwrap().to_string(),
            )
            .env("BOT_TOKEN", "tok-123")
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut pipe = relay.stderr.take().unwrap();
        let stderr = thread::spawn(move || {
            let mut text = String::new();
            let _ = pipe.read_to_string(&mut text);
            text
        });
        listener.set_nonblocking(true).unwrap();
        let started = Instant::now();
        let conn = loop {
            match listener.accept() {
                Ok((conn, _)) => break conn,
                Err(e) if e.kind() == ErrorKind::WouldBlock && started.elapsed() < WAIT => {
                    thread::sleep(Duration::from_millis(10))
                }
                Err(e) => panic!("the relay didn't connect: {e}"),
            }
        };
        conn.set_nonblocking(false).unwrap();
        conn.set_read_timeout(Some(WAIT)).unwrap();
        let reader = BufReader::new(conn.try_clone().unwrap());
        Harness {
            relay,
            conn,
            reader,
            stderr,
        }
    }

    fn send(&mut self, msg: Value) {
        self.conn.write_all(format!("{msg}\n").as_bytes()).unwrap();
    }

    fn recv(&mut self) -> Value {
        let mut line = String::new();
        assert!(
            self.reader.read_line(&mut line).unwrap() > 0,
            "the relay closed the connection"
        );
        serde_json::from_str(&line).unwrap()
    }

    fn nothing_within(&mut self, quiet: Duration) -> bool {
        self.conn.set_read_timeout(Some(quiet)).unwrap();
        let mut line = String::new();
        let quiet =
            matches!(self.reader.read_line(&mut line), Err(e) if e.kind() == ErrorKind::WouldBlock);
        self.conn.set_read_timeout(Some(WAIT)).unwrap();
        quiet
    }

    fn handshake(&mut self) {
        assert_eq!(
            self.recv(),
            json!({"type": "hello", "token": "tok-123", "protocol": 1})
        );
        self.send(
            json!({"type": "init", "slot": 0, "players": 2, "config": {}, "budget_ms": 1000}),
        );
        assert_eq!(self.recv(), json!({"type": "ready"}));
    }

    fn turn(&mut self, turn: u32, state: Value) {
        self.send(json!({"type": "turn", "turn": turn, "state": state, "budget_ms": 1000}));
    }

    /// Closes the controller side and returns how long the relay took to exit, and its report.
    fn close(self) -> (Duration, Value) {
        let _ = self.conn.shutdown(std::net::Shutdown::Both);
        let (mut relay, started) = (self.relay, Instant::now());
        loop {
            if relay.try_wait().unwrap().is_some() {
                break;
            }
            assert!(
                started.elapsed() < Duration::from_secs(10),
                "the relay didn't exit"
            );
            thread::sleep(Duration::from_millis(20));
        }
        let stderr = self.stderr.join().unwrap();
        let report = stderr
            .lines()
            .rev()
            .find_map(|l| serde_json::from_str::<Value>(l).ok());
        (
            started.elapsed(),
            report.unwrap_or_else(|| panic!("no report in {stderr:?}")),
        )
    }
}

#[test]
fn hello_comes_first_and_answers_carry_their_turn_number() {
    let mut h = Harness::start(ECHO_BOT);
    h.handshake();
    h.turn(1, json!({"x": 1}));
    assert_eq!(
        h.recv(),
        json!({"type": "action", "turn": 1, "action": {"echo": {"x": 1}, "env": []}})
    );
    h.turn(2, json!({"x": 2}));
    assert_eq!(h.recv()["turn"], 2);
    h.send(json!({"type": "end", "rank": 1, "score": 3}));
    let (_, report) = h.close();
    assert_eq!(report["bot_exit"], "exit code 0", "{report}");
}

#[test]
fn the_bot_never_sees_the_token_or_the_controller_address() {
    let mut h = Harness::start(ECHO_BOT);
    h.handshake();
    h.turn(1, json!({}));
    assert_eq!(h.recv()["action"]["env"], json!([]));
}

#[test]
fn output_that_is_not_json_is_marked_invalid() {
    let mut h = Harness::start("import sys\nfor line in sys.stdin: print('not json', flush=True)");
    h.handshake();
    h.turn(1, json!({}));
    assert_eq!(
        h.recv(),
        json!({"type": "action", "turn": 1, "invalid": true})
    );
}

#[test]
fn a_late_answer_keeps_its_own_turn_number() {
    let bot = r#"
import json, sys, time
sys.stdin.readline(); print("ready", flush=True)
for line in sys.stdin:
    turn = json.loads(line)["turn"]
    if turn == 1: time.sleep(0.3)
    print(json.dumps({"for": turn}), flush=True)
"#;
    let mut h = Harness::start(bot);
    h.handshake();
    h.turn(1, json!({}));
    h.turn(2, json!({})); // sent before the bot answered turn 1
    assert_eq!(
        h.recv(),
        json!({"type": "action", "turn": 1, "action": {"for": 1}})
    );
    assert_eq!(
        h.recv(),
        json!({"type": "action", "turn": 2, "action": {"for": 2}})
    );
}

#[test]
fn an_unflushed_answer_never_arrives() {
    let bot = r#"
import os, sys
out = os.fdopen(1, "w", buffering=65536)
sys.stdin.readline(); out.write("ready\n")
import time; time.sleep(30)
"#;
    let mut h = Harness::start(bot);
    assert_eq!(h.recv()["type"], "hello");
    h.send(json!({"type": "init", "slot": 0, "players": 2, "config": {}, "budget_ms": 1000}));
    assert!(
        h.nothing_within(Duration::from_secs(1)),
        "the bot never flushed, so nothing may arrive"
    );
}

#[test]
fn a_bot_that_ignores_eof_is_killed_two_seconds_after_the_controller_closes() {
    let bot = r#"
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.stdin.readline(); print("ready", flush=True)
time.sleep(60)
"#;
    let mut h = Harness::start(bot);
    h.handshake();
    let (took, report) = h.close();
    assert!(
        took >= Duration::from_millis(1_800) && took < Duration::from_secs(5),
        "took {took:?}"
    );
    assert!(
        report["bot_exit"].as_str().unwrap().contains("killed"),
        "{report}"
    );
}

#[test]
fn a_bot_that_exits_closes_the_connection() {
    let mut h = Harness::start("import sys\nsys.stdin.readline(); print('ready', flush=True)");
    h.handshake();
    let mut line = String::new();
    assert_eq!(
        h.reader.read_line(&mut line).unwrap(),
        0,
        "expected the relay to close, got {line:?}"
    );
}

#[test]
fn a_stderr_flood_is_capped_and_the_relay_keeps_answering() {
    let bot = r#"
import sys
sys.stdin.readline()
sys.stderr.write("e" * 5_000_000); sys.stderr.flush()
print("ready", flush=True)
"#;
    let mut h = Harness::start(bot);
    h.handshake();
    let (_, report) = h.close();
    assert_eq!(report["stderr_bytes"], 5_000_000);
    assert_eq!(report["stderr_truncated"], true);
    assert_eq!(report["stderr"].as_str().unwrap().len(), 64 * 1024);
}

#[test]
fn an_oversized_line_is_passed_on_so_the_controller_can_reject_it() {
    let bot = r#"
import sys
sys.stdin.readline(); print("ready", flush=True)
sys.stdin.readline(); print("x" * 70_000, flush=True)
import time; time.sleep(30)
"#;
    let mut h = Harness::start(bot);
    h.handshake();
    h.turn(1, json!({}));
    let mut raw = Vec::new();
    (&mut h.reader)
        .take(MAX_LINE as u64 + 1)
        .read_until(b'\n', &mut raw)
        .unwrap();
    assert_eq!(raw.len(), MAX_LINE + 1);
    assert!(!raw.ends_with(b"\n"));
}

#[test]
fn the_relay_uses_no_cpu_while_the_bot_thinks() {
    let mut h = Harness::start(
        "import sys, time\nsys.stdin.readline(); print('ready', flush=True); time.sleep(30)",
    );
    h.handshake();
    let cpu_ticks = |pid: u32| -> u64 {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
        let fields: Vec<&str> = stat
            .rsplit(')')
            .next()
            .unwrap()
            .split_whitespace()
            .collect();
        fields[11].parse::<u64>().unwrap() + fields[12].parse::<u64>().unwrap() // utime + stime
    };
    let before = cpu_ticks(h.relay.id());
    thread::sleep(Duration::from_secs(2));
    let used = cpu_ticks(h.relay.id()) - before;
    assert!(
        used <= 2,
        "the relay used {used} CPU ticks in 2 idle seconds"
    );
}

#[test]
fn missing_configuration_is_reported() {
    let out = Command::new(env!("CARGO_BIN_EXE_relay"))
        .args(["--", "true"])
        .env_remove("BOT_TOKEN")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("BOT_TOKEN"));
}
