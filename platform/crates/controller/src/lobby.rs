//! Accepting connections and mapping them to slots by single-use token.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use protocol::{FromBot, MAX_LINE, MatchSpec, Outcome, PROTOCOL_VERSION};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc};
use tokio::time::{Instant, sleep, timeout, timeout_at};
use tracing::{info, warn};

use crate::player::Player;

/// A new connection must send `hello` within this time.
const HELLO_TIMEOUT: Duration = Duration::from_secs(2);
/// Connections that haven't authenticated yet; more are closed right away.
const MAX_HANDSHAKES: usize = 16;

struct Authenticated {
    slot: usize,
    reader: BufReader<OwnedReadHalf>,
    writer: OwnedWriteHalf,
    addr: SocketAddr,
}

type Unclaimed = Arc<Mutex<HashMap<String, usize>>>;

/// Waits until every player has connected or `connect_ms` has passed, then stops listening.
/// `init` is only sent after this, once all players are in.
pub(crate) async fn connect_players(
    listener: TcpListener,
    spec: &MatchSpec,
    players: &mut [Player],
) {
    let deadline = Instant::now() + Duration::from_millis(spec.limits.connect_ms);
    let unclaimed: Unclaimed = Arc::new(Mutex::new(
        spec.players
            .iter()
            .map(|p| (p.token.clone(), p.slot))
            .collect(),
    ));
    let (tx, mut rx) = mpsc::unbounded_channel();
    let accept = tokio::spawn(accept_loop(listener, unclaimed.clone(), tx));

    let mut waiting = players.len();
    while waiting > 0 {
        let Ok(Some(bot)) = timeout_at(deadline, rx.recv()).await else {
            break;
        };
        players[bot.slot].attach(bot.reader, bot.writer);
        info!(slot = bot.slot, addr = %bot.addr, "bot connected");
        waiting -= 1;
    }

    accept.abort(); // drops the listener
    unclaimed.lock().expect("lock").clear();
    for p in players.iter_mut().filter(|p| !p.connected()) {
        p.forfeit(Outcome::NeverConnected);
    }
}

async fn accept_loop(
    listener: TcpListener,
    unclaimed: Unclaimed,
    tx: mpsc::UnboundedSender<Authenticated>,
) {
    let handshakes = Arc::new(Semaphore::new(MAX_HANDSHAKES));
    loop {
        let (stream, addr) = match listener.accept().await {
            Ok(conn) => conn,
            Err(e) => {
                warn!(error = %e, "accept failed");
                sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        match handshakes.clone().try_acquire_owned() {
            Ok(permit) => {
                tokio::spawn(handshake(
                    stream,
                    addr,
                    unclaimed.clone(),
                    tx.clone(),
                    permit,
                ));
            }
            Err(_) => warn!(%addr, "rejected connection: too many unauthenticated connections"),
        }
    }
}

async fn handshake(
    stream: TcpStream,
    addr: SocketAddr,
    unclaimed: Unclaimed,
    tx: mpsc::UnboundedSender<Authenticated>,
    _permit: OwnedSemaphorePermit,
) {
    let _ = stream.set_nodelay(true);
    let (reader, writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut line = Vec::new();
    let read = timeout(
        HELLO_TIMEOUT,
        (&mut reader)
            .take(MAX_LINE as u64 + 1)
            .read_until(b'\n', &mut line),
    )
    .await;

    let reason = match read {
        Err(_) => "no hello within 2 s",
        Ok(Err(_)) | Ok(Ok(0)) => "closed before hello",
        Ok(Ok(_)) if line.last() != Some(&b'\n') => "hello too long",
        Ok(Ok(_)) => match serde_json::from_slice::<FromBot>(&line) {
            Ok(FromBot::Hello { protocol, .. }) if protocol != PROTOCOL_VERSION => {
                "unsupported protocol version"
            }
            Ok(FromBot::Hello { token, .. }) => {
                let slot = unclaimed.lock().expect("lock").remove(&token); // single use
                match slot {
                    Some(slot) => {
                        let _ = tx.send(Authenticated {
                            slot,
                            reader,
                            writer,
                            addr,
                        });
                        return;
                    }
                    None => "unknown or already used token",
                }
            }
            _ => "not a valid hello",
        },
    };
    // Never log the token itself.
    warn!(%addr, reason, "rejected connection");
}
