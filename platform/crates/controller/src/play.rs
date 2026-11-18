//! The match: init, turns, end, and what happens when it's cut short.

use std::collections::HashMap;
use std::net::Ipv4Addr;
use std::process::ExitCode;
use std::time::Duration;

use protocol::{
    Limits, MatchResult, MatchSpec, Outcome, PlayerResult, Replay, ReplayTurn, Status, ToBot,
    TurnTime,
};
use tokio::net::TcpListener;
use tokio::signal::unix::{SignalKind, signal};
use tokio::time::Instant;
use tracing::{error, info, warn};

use crate::player::{Answer, Expect, Player, Reply, encode, send_all};
use crate::{CONTROLLER_VERSION, Config, Game, lobby, output, panics};

/// How often a running match logs how far it is: a long match is otherwise silent, and from the
/// outside it can't be told apart from a stuck one.
const PROGRESS_INTERVAL: Duration = Duration::from_secs(60);

pub(crate) async fn run_match<G: Game>(mut game: G, config: Config) -> ExitCode {
    let spec = &config.spec;
    let mut players: Vec<Player> = (0..spec.players.len()).map(Player::new).collect();
    let mut replay = Replay {
        match_id: spec.match_id.clone(),
        attempt: spec.attempt,
        seed: spec.seed,
        num_players: players.len(),
        game_config: spec.game.clone(),
        limits: spec.limits,
        controller_version: CONTROLLER_VERSION.into(),
        game_version: config.game_version.clone(),
        turns: Vec::new(),
        final_scores: None,
        result: None,
    };

    let listener = match TcpListener::bind((Ipv4Addr::UNSPECIFIED, config.port)).await {
        Ok(l) => l,
        Err(e) => {
            error!(port = config.port, error = %e, "cannot listen");
            return ExitCode::from(2);
        }
    };
    // From here on SIGTERM no longer kills the process: it aborts the match, and the
    // replay and result are still written.
    let mut sigterm = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(e) => {
            error!(error = %e, "cannot handle SIGTERM");
            return ExitCode::from(1);
        }
    };
    // The bound port, which differs from PORT when PORT=0 (any free port; the tests use that).
    let port = listener.local_addr().map_or(config.port, |a| a.port());
    info!(match_id = %spec.match_id, attempt = spec.attempt, port, players = players.len(), "listening");

    let mut standings = None;
    let status = tokio::select! {
        played = play(&mut game, &mut players, listener, spec, &mut replay) => match played {
            Ok(s) => {
                standings = Some(s);
                Status::Completed
            }
            Err(e) => {
                error!(error = %e, "match failed: it will be voided");
                Status::ControllerError
            }
        },
        _ = sigterm.recv() => {
            warn!("SIGTERM: aborting the match");
            Status::Aborted
        }
    };

    for p in &mut players {
        p.close().await; // the relays then stop their bots
    }
    let result = MatchResult {
        match_id: spec.match_id.clone(),
        attempt: spec.attempt,
        status,
        turns: replay.turns.len() as u32,
        controller_version: CONTROLLER_VERSION.into(),
        game_version: config.game_version.clone(),
        players: players
            .iter()
            .map(|p| PlayerResult {
                slot: p.slot,
                rank: standings
                    .as_ref()
                    .map(|(_, ranks): &(Vec<i64>, Vec<u32>)| ranks[p.slot]),
                score: standings.as_ref().map(|(scores, _)| scores[p.slot]),
                outcome: p.outcome,
                timeouts: p.timeouts,
                invalid_actions: p.invalid_actions,
            })
            .collect(),
    };
    replay.result = Some(result.clone());
    if let Err(e) = output::write(&config.out_dir, &replay, &result) {
        error!(error = %e, out_dir = %config.out_dir.display(), "cannot write the result");
        return ExitCode::from(1);
    }
    info!(match_id = %spec.match_id, status = ?status, turns = result.turns, "match finished");
    if status == Status::Completed {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

/// Plays the match. Returns the final scores and ranks, or an error that voids the match.
async fn play<G: Game>(
    game: &mut G,
    players: &mut [Player],
    listener: TcpListener,
    spec: &MatchSpec,
    replay: &mut Replay,
) -> Result<(Vec<i64>, Vec<u32>), String> {
    let limits = &spec.limits;
    let n = players.len();
    let grace = Duration::from_millis(limits.grace_ms);
    lobby::connect_players(listener, spec, players).await;

    let infos = game_call("init_info", || {
        (0..n).map(|s| game.init_info(s)).collect::<Vec<_>>()
    })?;
    let everyone: Vec<usize> = (0..n).collect();
    let init_budget = Duration::from_millis(limits.init_ms) + grace;
    let build_init = |s: usize| ToBot::Init {
        slot: s,
        players: n,
        config: infos[s].clone(),
        budget_ms: limits.init_ms,
    };
    for (slot, reply) in ask(players, &everyone, build_init, init_budget, Expect::Ready).await {
        match reply {
            Reply::Timeout => players[slot].forfeit(Outcome::Timeout),
            Reply::Answer(Answer::Malformed, _) => count_invalid(&mut players[slot], limits),
            _ => {}
        }
    }

    let started = Instant::now();
    let turn_budget = Duration::from_millis(limits.turn_ms) + grace;
    let mut validate_panic_logged = false;
    let mut last_progress = started;
    for turn in 1..=limits.max_turns {
        if game_call("is_over", || game.is_over())? {
            break;
        }
        if last_progress.elapsed() >= PROGRESS_INTERVAL {
            last_progress = Instant::now();
            info!(
                turn,
                max_turns = limits.max_turns,
                elapsed_s = started.elapsed().as_secs(),
                "match progress"
            );
        }
        if players.iter().filter(|p| p.active()).count() < n.min(2) {
            break; // not enough players left
        }
        if started.elapsed() > Duration::from_millis(limits.total_ms) {
            info!(turn, "total match time used up");
            break;
        }

        let movers = game_call("players_to_move", || game.players_to_move())?;
        check_movers(&movers, n)?;
        let states: HashMap<usize, _> = game_call("state", || {
            movers.iter().map(|&s| (s, game.state(s))).collect()
        })?;
        let build_turn = |s: usize| ToBot::Turn {
            turn,
            state: states[&s].clone(),
            budget_ms: limits.turn_ms,
        };
        let replies = ask(
            players,
            &movers,
            build_turn,
            turn_budget,
            Expect::Action(turn),
        )
        .await;

        let mut applied = Vec::with_capacity(movers.len());
        let mut actions = vec![None; n];
        let mut times = vec![None; n];
        for (slot, reply) in replies {
            let p = &mut players[slot];
            let mut valid = None;
            match reply {
                Reply::Inactive => {}
                Reply::Timeout => {
                    p.timeouts += 1;
                    times[slot] = Some(TurnTime::Timeout);
                    if p.timeouts >= limits.max_timeouts {
                        p.forfeit(Outcome::Timeout);
                    }
                }
                Reply::Answer(answer, elapsed) => {
                    times[slot] = Some(TurnTime::Ms(elapsed));
                    if let Answer::Action {
                        action: Some(value),
                        invalid: false,
                    } = answer
                    {
                        match panics::quietly(|| game.validate(slot, &value)) {
                            Ok(Some(action)) => {
                                valid = Some(action);
                                actions[slot] = Some(value);
                            }
                            Ok(None) => {}
                            Err(panic) => {
                                // A bot must not be able to void a match it's losing by sending junk.
                                if !validate_panic_logged {
                                    validate_panic_logged = true;
                                    error!(slot, panic = %panic, "validate panicked (game bug, counted as an invalid action; logged once per match)");
                                }
                            }
                        }
                    }
                    if valid.is_none() {
                        count_invalid(p, limits);
                    }
                }
            }
            applied.push((slot, valid));
        }
        game_call("apply", || game.apply(&applied))?;
        replay.turns.push(ReplayTurn {
            turn,
            movers,
            actions,
            times_ms: times,
        });
    }

    let scores = game_call("scores", || game.scores())?;
    if scores.len() != n {
        return Err(format!(
            "scores() returned {} values for {n} players",
            scores.len()
        ));
    }
    replay.final_scores = Some(scores.clone());
    let ranks = compute_ranks(players, &scores);
    let ends = (0..n)
        .filter(|&s| players[s].alive)
        .map(|s| {
            (
                s,
                encode(&ToBot::End {
                    rank: ranks[s],
                    score: scores[s],
                }),
            )
        })
        .collect();
    send_all(players, ends).await;
    Ok((scores, ranks))
}

/// Sends one request to each active mover, then collects the replies in mover order.
async fn ask(
    players: &mut [Player],
    movers: &[usize],
    build: impl Fn(usize) -> ToBot,
    budget: Duration,
    expect: Expect,
) -> Vec<(usize, Reply)> {
    let mut lines = Vec::new();
    for &s in movers {
        players[s].drain_stale();
        if players[s].active() {
            lines.push((s, encode(&build(s))));
        }
    }
    let sent = send_all(players, lines).await;
    let mut replies = Vec::with_capacity(movers.len());
    for &s in movers {
        let reply = match sent.get(&s) {
            None => Reply::Inactive,
            Some(None) => {
                players[s].send_failed().await;
                Reply::Inactive
            }
            Some(Some(sent)) => players[s].await_reply(*sent, budget, expect).await,
        };
        replies.push((s, reply));
    }
    replies
}

fn count_invalid(p: &mut Player, limits: &Limits) {
    p.invalid_actions += 1;
    if p.invalid_actions >= limits.max_invalid {
        p.forfeit(Outcome::InvalidOutput);
    }
}

fn game_call<T>(what: &str, f: impl FnOnce() -> T) -> Result<T, String> {
    panics::quietly(f).map_err(|panic| format!("{what} panicked: {panic}"))
}

fn check_movers(movers: &[usize], n: usize) -> Result<(), String> {
    let mut seen = vec![false; n];
    if movers.is_empty() {
        return Err("players_to_move() returned nobody, but the game isn't over".into());
    }
    for &m in movers {
        if m >= n || std::mem::replace(&mut seen[m], true) {
            return Err(format!(
                "players_to_move() returned an invalid list: {movers:?}"
            ));
        }
    }
    Ok(())
}

/// Forfeited players rank below everyone else; otherwise a higher score is better.
/// Equal players share a rank (1, 1, 3).
fn compute_ranks(players: &[Player], scores: &[i64]) -> Vec<u32> {
    let key = |s: usize| (players[s].outcome != Outcome::Ok, -scores[s]);
    let mut order: Vec<usize> = (0..players.len()).collect();
    order.sort_by_key(|&s| key(s));
    let mut ranks = vec![0; players.len()];
    for (i, &s) in order.iter().enumerate() {
        ranks[s] = if i > 0 && key(order[i - 1]) == key(s) {
            ranks[order[i - 1]]
        } else {
            i as u32 + 1
        };
    }
    ranks
}
