//! One bot's connection: a reader task that timestamps every line on arrival, and sends
//! with one overall deadline per message.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use protocol::{FromBot, MAX_LINE, Outcome, ToBot};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::{Mutex, mpsc};
use tokio::task::JoinSet;
use tokio::time::{Instant, timeout, timeout_at};

/// A bot that doesn't take a whole message within this time is dropped as `disconnected`.
/// One deadline per message: a per-write timeout would reset on every partial write.
pub(crate) const SEND_DEADLINE: Duration = Duration::from_secs(5);
/// Lines queued per bot. A bot that floods us waits for its own backlog, nobody else does.
const INBOX: usize = 64;

pub(crate) enum Incoming {
    Msg(FromBot),
    /// Not JSON, nested too deeply, or not a valid message: counts as an invalid action.
    Malformed,
    /// The connection is gone; `Some(InvalidOutput)` for an oversized line.
    Closed(Option<Outcome>),
}

pub(crate) struct Arrival {
    pub at: Instant,
    pub what: Incoming,
}

#[derive(Clone, Copy)]
pub(crate) enum Expect {
    Ready,
    Action(u32),
}

pub(crate) enum Answer {
    Ready,
    Action {
        action: Option<Value>,
        invalid: bool,
    },
    Malformed,
}

pub(crate) enum Reply {
    Answer(Answer, f64),
    Timeout,
    Inactive,
}

/// When sending began and when the whole message was handed to the kernel.
#[derive(Clone, Copy)]
pub(crate) struct Sent {
    pub sending: Instant,
    pub done: Instant,
}

type Writer = Arc<Mutex<Option<OwnedWriteHalf>>>;

pub(crate) struct Player {
    pub slot: usize,
    inbox: Option<mpsc::Receiver<Arrival>>,
    writer: Option<Writer>,
    pub alive: bool,
    close_reason: Option<Outcome>,
    pub outcome: Outcome,
    pub timeouts: u32,
    pub invalid_actions: u32,
}

impl Player {
    pub fn new(slot: usize) -> Player {
        Player {
            slot,
            inbox: None,
            writer: None,
            alive: false,
            close_reason: None,
            outcome: Outcome::Ok,
            timeouts: 0,
            invalid_actions: 0,
        }
    }

    pub fn attach(&mut self, reader: BufReader<OwnedReadHalf>, writer: OwnedWriteHalf) {
        let writer = Arc::new(Mutex::new(Some(writer)));
        let (tx, rx) = mpsc::channel(INBOX);
        tokio::spawn(read_loop(reader, tx, writer.clone()));
        self.inbox = Some(rx);
        self.writer = Some(writer);
        self.alive = true;
    }

    pub fn connected(&self) -> bool {
        self.inbox.is_some()
    }

    /// Still in the match: connected and not forfeited.
    pub fn active(&self) -> bool {
        self.alive && self.outcome == Outcome::Ok
    }

    /// Keeps the first reason.
    pub fn forfeit(&mut self, outcome: Outcome) {
        if self.outcome == Outcome::Ok {
            self.outcome = outcome;
        }
    }

    fn mark_closed(&mut self, reason: Option<Outcome>) {
        self.alive = false;
        if self.close_reason.is_none() {
            self.close_reason = reason;
        }
        self.forfeit(self.close_reason.unwrap_or(Outcome::Disconnected));
    }

    /// Everything queued now arrived before the next request, so only a close matters.
    pub fn drain_stale(&mut self) {
        let Some(inbox) = self.inbox.as_mut() else {
            return;
        };
        let mut closed = None;
        loop {
            match inbox.try_recv() {
                Ok(Arrival {
                    what: Incoming::Closed(reason),
                    ..
                }) => closed = Some(reason),
                Ok(_) => {}
                Err(mpsc::error::TryRecvError::Empty) => break,
                Err(mpsc::error::TryRecvError::Disconnected) => {
                    closed = Some(closed.flatten());
                    break;
                }
            }
        }
        if let Some(reason) = closed {
            self.mark_closed(reason);
        }
    }

    pub async fn close(&mut self) {
        if let Some(writer) = &self.writer {
            writer.lock().await.take(); // dropping the write half sends FIN
        }
    }

    pub async fn send_failed(&mut self) {
        self.mark_closed(None);
        self.close().await;
    }

    /// Waits for the answer to the request described by `sent`. The bot's clock starts when
    /// its request was completely sent; the deadline is enforced while waiting.
    pub async fn await_reply(&mut self, sent: Sent, budget: Duration, expect: Expect) -> Reply {
        let deadline = sent.done + budget;
        loop {
            let next = match self.inbox.as_mut() {
                Some(inbox) => timeout_at(deadline, inbox.recv()).await,
                None => return Reply::Inactive,
            };
            let arrival = match next {
                Err(_) => return Reply::Timeout,
                Ok(None) => {
                    self.mark_closed(None);
                    return Reply::Inactive;
                }
                Ok(Some(arrival)) => arrival,
            };
            if arrival.at > deadline {
                return Reply::Timeout; // it came, but too late
            }
            // A fast bot can answer before `done` is recorded, so only lines that arrived
            // before sending *began* are stale. Time is measured from `done`, clamped to 0.
            let elapsed = arrival
                .at
                .saturating_duration_since(sent.done)
                .as_secs_f64()
                * 1000.0;
            let elapsed = (elapsed * 100.0).round() / 100.0;
            match arrival.what {
                Incoming::Closed(reason) => {
                    self.mark_closed(reason);
                    return Reply::Inactive;
                }
                _ if arrival.at < sent.sending => continue,
                Incoming::Malformed => return Reply::Answer(Answer::Malformed, elapsed),
                Incoming::Msg(FromBot::Ready) if matches!(expect, Expect::Ready) => {
                    return Reply::Answer(Answer::Ready, elapsed);
                }
                Incoming::Msg(FromBot::Action {
                    turn,
                    action,
                    invalid,
                }) if matches!(expect, Expect::Action(t) if t == turn) => {
                    return Reply::Answer(Answer::Action { action, invalid }, elapsed);
                }
                // A valid message that doesn't answer this request, e.g. a late answer to an
                // earlier turn: ignored.
                Incoming::Msg(_) => continue,
            }
        }
    }
}

/// Sends every line concurrently, so a bot that stopped reading can't delay the others.
/// Returns, per player index, when its send happened, or `None` if it failed.
pub(crate) async fn send_all(
    players: &[Player],
    lines: Vec<(usize, Vec<u8>)>,
) -> HashMap<usize, Option<Sent>> {
    let mut sends = JoinSet::new();
    for (index, line) in lines {
        let Some(writer) = players[index].writer.clone() else {
            continue;
        };
        sends.spawn(async move {
            let sending = Instant::now();
            let written = timeout(SEND_DEADLINE, async {
                match writer.lock().await.as_mut() {
                    Some(w) => w.write_all(&line).await.is_ok(),
                    None => false,
                }
            })
            .await
            .unwrap_or(false);
            (
                index,
                written.then(|| Sent {
                    sending,
                    done: Instant::now(),
                }),
            )
        });
    }
    let mut results = HashMap::new();
    while let Some(Ok((index, sent))) = sends.join_next().await {
        results.insert(index, sent);
    }
    results
}

pub(crate) fn encode(msg: &ToBot) -> Vec<u8> {
    let mut line = serde_json::to_vec(msg).expect("protocol messages always serialize");
    line.push(b'\n');
    line
}

/// Reads lines until the connection closes. Arrival times are taken here, so a busy match
/// loop never makes a bot late.
async fn read_loop(
    mut reader: BufReader<OwnedReadHalf>,
    tx: mpsc::Sender<Arrival>,
    writer: Writer,
) {
    let mut line = Vec::with_capacity(4096);
    loop {
        line.clear();
        let read = (&mut reader)
            .take(MAX_LINE as u64 + 1)
            .read_until(b'\n', &mut line)
            .await;
        let at = Instant::now();
        let what = match read {
            Ok(_) if line.last() == Some(&b'\n') => {
                match serde_json::from_slice::<FromBot>(&line) {
                    Ok(msg) => Incoming::Msg(msg),
                    // serde_json stops at 128 nesting levels with an error instead of crashing.
                    Err(_) => Incoming::Malformed,
                }
            }
            Ok(_) if line.len() > MAX_LINE => Incoming::Closed(Some(Outcome::InvalidOutput)),
            _ => Incoming::Closed(None), // closed, possibly mid-line, or a read error
        };
        let closed = matches!(what, Incoming::Closed(_));
        if closed {
            writer.lock().await.take();
        }
        if tx.send(Arrival { at, what }).await.is_err() || closed {
            return;
        }
    }
}
