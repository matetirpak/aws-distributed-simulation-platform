//! The relay, in every bot container: `ENTRYPOINT ["/relay", "--", <bot command>...]`.
//!
//! It connects to the game controller, authenticates with the token, then starts the bot
//! and translates: controller lines go to the bot's stdin unchanged, and each line the bot
//! prints answers the oldest open request. Contestants never write socket code.
//! Contract: `docs/contracts.md` ("Relay ⇄ bot").
//!
//! The relay is **not** a security boundary: it shares the container with the bot, which
//! can take it over. The controller treats everything from here as hostile.
//!
//! Plain threads with blocking I/O: nothing ever busy-waits, so the bot keeps the CPU.

use std::collections::VecDeque;
use std::env;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::os::unix::process::ExitStatusExt;
use std::process::{
    Child, ChildStderr, ChildStdin, ChildStdout, Command, ExitCode, ExitStatus, Stdio,
};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use protocol::{FromBot, MAX_LINE, PROTOCOL_VERSION, ToBotHeader};
use serde_json::{Value, json};

/// Longest line passed from the controller to the bot (a turn carries the full state).
const TO_BOT_MAX: usize = 16 * 1024 * 1024;
/// Bot stderr kept as debug info for the contestant; the rest is read and dropped.
const STDERR_CAP: usize = 64 * 1024;
/// After stdin closes, the bot gets this long to exit before it's killed.
const KILL_AFTER: Duration = Duration::from_secs(2);
/// The controller may still be starting when the bot task is.
const CONNECT_FOR: Duration = Duration::from_secs(120);

enum Pending {
    Init,
    Turn(u32),
}

enum Event {
    ControllerClosed,
    BotStdoutClosed,
}

type Queue = Arc<Mutex<VecDeque<Pending>>>;

fn main() -> ExitCode {
    let mut command: Vec<String> = env::args().skip(1).collect();
    if command.first().map(String::as_str) == Some("--") {
        command.remove(0);
    }
    if command.is_empty() {
        return fail("usage: relay -- <bot command> [args...]");
    }
    let (Ok(addr), Ok(token)) = (env::var("CONTROLLER_ADDR"), env::var("BOT_TOKEN")) else {
        return fail("CONTROLLER_ADDR and BOT_TOKEN must be set");
    };

    // Authenticate before the bot exists, then start it without the address and the token.
    let mut socket = match connect(&addr) {
        Ok(s) => s,
        Err(e) => return fail(&format!("cannot connect to the controller at {addr}: {e}")),
    };
    if let Err(e) = send(
        &mut socket,
        &FromBot::Hello {
            token,
            protocol: PROTOCOL_VERSION,
        },
    ) {
        return fail(&format!("cannot send hello: {e}"));
    }
    let mut bot = match Command::new(&command[0])
        .args(&command[1..])
        .env_remove("CONTROLLER_ADDR")
        .env_remove("BOT_TOKEN")
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(e) => return fail(&format!("cannot start the bot {:?}: {e}", command[0])),
    };

    let pending: Queue = Arc::default();
    let (events, events_rx) = mpsc::channel();
    let (to_bot, to_bot_rx) = mpsc::channel();
    let (Ok(read_side), Ok(write_side)) = (socket.try_clone(), socket.try_clone()) else {
        return fail("cannot share the controller connection between threads");
    };
    let stdin = bot.stdin.take().expect("piped");
    let stdout = bot.stdout.take().expect("piped");
    let stderr = Arc::new(Mutex::new(Captured::default()));
    let (stderr_done, stderr_done_rx) = mpsc::channel();

    thread::spawn({
        let (pending, events) = (pending.clone(), events.clone());
        move || from_controller(read_side, pending, to_bot, events)
    });
    thread::spawn(move || write_to_bot(stdin, to_bot_rx));
    thread::spawn({
        let events = events.clone();
        move || to_controller(stdout, write_side, pending, events)
    });
    thread::spawn({
        let (pipe, stderr) = (bot.stderr.take().expect("piped"), stderr.clone());
        move || {
            capture_stderr(pipe, &stderr);
            let _ = stderr_done.send(());
        }
    });
    drop(events);

    // Whichever side finishes first ends the match for this bot.
    if let Ok(Event::BotStdoutClosed) = events_rx.recv() {
        let _ = socket.shutdown(Shutdown::Both); // the controller sees the bot leave
    }
    let exit = wait_or_kill(&mut bot);
    let _ = socket.shutdown(Shutdown::Both);
    // A process the bot started may still hold stderr open; don't wait for it forever.
    let _ = stderr_done_rx.recv_timeout(Duration::from_millis(500));
    report(&exit, &stderr.lock().expect("lock"));
    ExitCode::SUCCESS
}

fn connect(addr: &str) -> io::Result<TcpStream> {
    let started = Instant::now();
    loop {
        match TcpStream::connect(addr) {
            Ok(socket) => {
                let _ = socket.set_nodelay(true);
                return Ok(socket);
            }
            Err(_) if started.elapsed() < CONNECT_FOR => thread::sleep(Duration::from_millis(250)),
            Err(e) => return Err(e),
        }
    }
}

fn send(socket: &mut TcpStream, msg: &FromBot) -> io::Result<()> {
    let mut line = serde_json::to_vec(msg)?;
    line.push(b'\n');
    socket.write_all(&line)
}

/// Controller → bot. Every request is queued *before* the bot can see it, so the answer
/// that follows is always matched to the right turn.
fn from_controller(
    socket: TcpStream,
    pending: Queue,
    to_bot: Sender<Vec<u8>>,
    events: Sender<Event>,
) {
    let mut reader = BufReader::new(socket);
    let mut to_bot = Some(to_bot);
    let mut line = Vec::new();
    loop {
        line.clear();
        match (&mut reader)
            .take(TO_BOT_MAX as u64 + 1)
            .read_until(b'\n', &mut line)
        {
            Ok(_) if line.last() == Some(&b'\n') => {}
            _ => break, // closed, failed, or longer than any state should be
        }
        let header = serde_json::from_slice::<ToBotHeader>(&line).ok();
        let (kind, turn) = header
            .as_ref()
            .map_or(("", None), |h| (h.kind.as_str(), h.turn));
        match (kind, turn) {
            ("init", _) => pending.lock().expect("lock").push_back(Pending::Init),
            ("turn", Some(turn)) => pending.lock().expect("lock").push_back(Pending::Turn(turn)),
            _ => {}
        }
        if let Some(tx) = &to_bot {
            let _ = tx.send(line.clone());
        }
        if kind == "end" {
            to_bot = None; // after `end`, the bot's stdin closes: the match is over
        }
    }
    drop(to_bot);
    let _ = events.send(Event::ControllerClosed);
}

/// A separate thread, so a bot that doesn't read its stdin never stops us from noticing
/// that the controller has closed the connection.
fn write_to_bot(mut stdin: ChildStdin, lines: Receiver<Vec<u8>>) {
    for line in lines {
        if stdin.write_all(&line).and_then(|()| stdin.flush()).is_err() {
            return;
        }
    }
    // Dropping stdin here is the bot's EOF.
}

/// Bot → controller: each output line answers the oldest open request.
fn to_controller(
    stdout: ChildStdout,
    mut socket: TcpStream,
    pending: Queue,
    events: Sender<Event>,
) {
    let mut reader = BufReader::new(stdout);
    let mut line = Vec::new();
    loop {
        line.clear();
        match (&mut reader)
            .take(MAX_LINE as u64 + 1)
            .read_until(b'\n', &mut line)
        {
            Ok(0) | Err(_) => break,
            Ok(_) if line.last() != Some(&b'\n') && line.len() > MAX_LINE => {
                // Over the limit: pass it on, so the controller applies its own rule
                // (invalid_output, connection closed). Its close then ends this bot.
                let _ = socket.write_all(&line);
                return;
            }
            Ok(_) => {}
        }
        let text = line.strip_suffix(b"\n").unwrap_or(&line);
        let text = text.strip_suffix(b"\r").unwrap_or(text);
        let Some(request) = pending.lock().expect("lock").pop_front() else {
            continue; // output nobody asked for
        };
        let reply = match request {
            Pending::Init => FromBot::Ready, // any line means ready
            Pending::Turn(turn) => match serde_json::from_slice::<Value>(text) {
                Ok(action) => FromBot::Action {
                    turn,
                    action: Some(action),
                    invalid: false,
                },
                Err(_) => FromBot::Action {
                    turn,
                    action: None,
                    invalid: true,
                },
            },
        };
        if send(&mut socket, &reply).is_err() {
            break;
        }
    }
    let _ = events.send(Event::BotStdoutClosed);
}

#[derive(Default)]
struct Captured {
    kept: Vec<u8>,
    total: u64,
}

/// Keeps reading until EOF, so the bot never blocks on a full stderr pipe.
fn capture_stderr(mut pipe: ChildStderr, captured: &Mutex<Captured>) {
    let mut buf = [0u8; 8192];
    loop {
        match pipe.read(&mut buf) {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                let mut c = captured.lock().expect("lock");
                c.total += n as u64;
                let room = STDERR_CAP.saturating_sub(c.kept.len());
                c.kept.extend_from_slice(&buf[..n.min(room)]);
            }
        }
    }
}

fn wait_or_kill(bot: &mut Child) -> String {
    let deadline = Instant::now() + KILL_AFTER;
    loop {
        match bot.try_wait() {
            Ok(Some(status)) => return describe(status),
            // Polling only happens during these last 2 seconds of shutdown.
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = bot.kill();
                let status = bot.wait().map_or_else(|_| "unknown".to_string(), describe);
                return format!(
                    "{status} (killed {}s after the match ended)",
                    KILL_AFTER.as_secs()
                );
            }
        }
    }
}

fn describe(status: ExitStatus) -> String {
    match (status.code(), status.signal()) {
        (Some(code), _) => format!("exit code {code}"),
        (None, Some(signal)) => format!("signal {signal}"),
        _ => "unknown".into(),
    }
}

/// Debug info for the contestant: one JSON line on the relay's stderr. How it reaches the
/// contestant in AWS is still open.
fn report(exit: &str, stderr: &Captured) {
    let line = json!({
        "relay": "bot finished",
        "bot_exit": exit,
        "stderr_bytes": stderr.total,
        "stderr_truncated": stderr.total > stderr.kept.len() as u64,
        "stderr": String::from_utf8_lossy(&stderr.kept),
    });
    eprintln!("{line}");
}

fn fail(message: &str) -> ExitCode {
    eprintln!("{}", json!({"relay": "error", "message": message}));
    ExitCode::from(2)
}
