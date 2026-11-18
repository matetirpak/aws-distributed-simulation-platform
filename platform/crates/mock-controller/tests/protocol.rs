//! Protocol tests: the real controller binary, scripted fake bots, real TCP on localhost.
//!
//! Rules:
//! - never sleep to make a bot miss a deadline; wait for the controller's next message,
//!   which proves the timeout happened, and let `autoplay` answer for the other bot
//! - connect every bot before waiting for `init`
//! - end matches with `max_turns`, not with disconnects

mod common;

use std::time::{Duration, Instant};

use common::{Match, autoplay, run_to_exit, stay, take_one};
use protocol::{Outcome, Status, TurnTime};
use serde_json::{Value, json};

const CLOSE_WAIT: Duration = Duration::from_secs(5);

#[test]
fn full_coin_match_follows_the_protocol_and_its_replay_verifies() {
    let m = Match::start("coin", json!({"max_turns": 3}));
    let tokens = m.tokens.clone();
    let (mut a, mut b) = (m.bot(0), m.bot(1));
    let init = a.join();
    b.join();
    assert_eq!(init["slot"], 0);
    assert_eq!(init["players"], 2);
    assert!(init["config"]["board"].is_array());
    for turn in 1..=3u64 {
        for bot in [&mut a, &mut b] {
            assert_eq!(bot.expect("turn")["turn"], turn);
            bot.action(turn, json!({"move": "STAY"}));
        }
    }
    assert_eq!(a.expect("end")["rank"], 1); // nobody moved: a tie
    assert_eq!(b.expect("end")["rank"], 1);
    assert!(a.is_closed_by_server(CLOSE_WAIT) && b.is_closed_by_server(CLOSE_WAIT));

    let f = m.finish();
    assert_eq!(
        (f.code, f.result.status, f.result.turns),
        (0, Status::Completed, 3)
    );
    games::verify("coin", &f.replay).unwrap();
    assert!(
        tokens.iter().all(|t| !f.logs.contains(t.as_str())),
        "a token was logged"
    );
}

#[test]
fn nim_turns_alternate_and_the_last_taker_wins() {
    let m = Match::start_with("nim", json!({"heaps": [2, 3]}), json!({}));
    let a = autoplay(m.bot(0), take_one);
    let b = autoplay(m.bot(1), take_one);
    let (end_a, end_b) = (a.join().unwrap().unwrap(), b.join().unwrap().unwrap());
    // 5 objects, one per turn: slot 0 takes the 1st, 3rd and 5th, so it takes the last one.
    assert_eq!(
        (end_a["rank"].clone(), end_b["rank"].clone()),
        (json!(1), json!(2))
    );

    let f = m.finish();
    assert_eq!(f.result.status, Status::Completed);
    let movers: Vec<Vec<usize>> = f.replay.turns.iter().map(|t| t.movers.clone()).collect();
    assert_eq!(movers, vec![vec![0], vec![1], vec![0], vec![1], vec![0]]);
    games::verify("nim", &f.replay).unwrap();
}

#[test]
fn a_late_answer_is_a_timeout_and_is_not_applied_to_the_next_turn() {
    let m = Match::start("coin", json!({"max_turns": 2}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    assert_eq!(b.expect("turn")["turn"], 1); // b doesn't answer turn 1...
    assert_eq!(b.expect("turn")["turn"], 2); // ...so the controller times it out and moves on
    b.action(1, json!({"move": "N"})); // late answer to turn 1: ignored
    b.action(2, json!({"move": "STAY"}));
    b.expect("end");
    a.join().unwrap();

    let f = m.finish();
    let (turn1, turn2) = (&f.replay.turns[0], &f.replay.turns[1]);
    assert_eq!(turn1.times_ms[1], Some(TurnTime::Timeout));
    assert_eq!(turn1.actions[1], None);
    assert_eq!(
        turn2.actions[1],
        Some(json!({"move": "STAY"})),
        "turn 2 must use b's turn-2 answer, not the late N"
    );
    assert_eq!(f.result.players[1].timeouts, 1);
    assert_eq!(
        f.result.players[1].outcome,
        Outcome::Ok,
        "one timeout is not a forfeit"
    );
}

#[test]
fn bad_hellos_are_rejected_without_using_up_the_token() {
    let m = Match::start("coin", json!({"max_turns": 1, "connect_ms": 15_000}));
    let mut wrong = m.connect();
    wrong.hello("not-a-real-token");
    assert!(wrong.is_closed_by_server(CLOSE_WAIT));

    let mut a = m.bot(0);
    std::thread::sleep(Duration::from_millis(200)); // let the controller claim token 0 first
    let mut reused = m.bot(0); // tokens are single-use
    assert!(reused.is_closed_by_server(CLOSE_WAIT));

    let mut silent = m.connect(); // never says hello: dropped after 2 s
    let started = Instant::now();
    assert!(silent.is_closed_by_server(Duration::from_secs(4)));
    assert!(started.elapsed() >= Duration::from_millis(1_500));

    let mut future = m.connect(); // a valid token with an unknown protocol version
    future.send(json!({"type": "hello", "token": m.tokens[1], "protocol": 99}));
    assert!(future.is_closed_by_server(CLOSE_WAIT));
    let mut listy = m.connect();
    listy.send(json!({"type": "hello", "token": [m.tokens[1]], "protocol": 1}));
    assert!(listy.is_closed_by_server(CLOSE_WAIT));
    let mut garbage = m.connect();
    garbage.send_raw(b"GET / HTTP/1.1\r\n\r\n");
    assert!(garbage.is_closed_by_server(CLOSE_WAIT));

    let mut b = m.bot(1); // token 1 still works: the rejected attempts didn't use it up
    a.join();
    b.join();
    for bot in [&mut a, &mut b] {
        assert_eq!(bot.expect("turn")["turn"], 1);
        bot.action(1, json!({"move": "STAY"}));
    }
    a.expect("end");
    b.expect("end");
    let f = m.finish();
    assert_eq!(f.code, 0);
    assert!(f.result.players.iter().all(|p| p.outcome == Outcome::Ok));
}

#[test]
fn an_oversized_line_is_invalid_output_and_closes_the_connection() {
    let m = Match::start("coin", json!({}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    assert_eq!(b.expect("turn")["turn"], 1);
    b.send_raw(format!("{{\"pad\":\"{}\"}}\n", "x".repeat(70_000)).as_bytes()); // limit: 64 KiB
    assert!(b.is_closed_by_server(CLOSE_WAIT));
    assert_eq!(a.join().unwrap().unwrap()["rank"], 1);
    assert_eq!(m.finish().result.players[1].outcome, Outcome::InvalidOutput);
}

#[test]
fn malformed_and_deeply_nested_json_count_as_invalid_actions() {
    let m = Match::start("coin", json!({"max_invalid": 2}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    assert_eq!(b.expect("turn")["turn"], 1);
    b.send_raw(b"this is not json\n");
    assert_eq!(b.expect("turn")["turn"], 2);
    let depth = 10_000; // about 20 KB: under the line limit, but far too deep for the parser
    b.send_raw(
        format!(
            "{{\"type\":\"action\",\"turn\":2,\"action\":{}{}}}\n",
            "[".repeat(depth),
            "]".repeat(depth)
        )
        .as_bytes(),
    );
    assert_eq!(b.expect("end")["rank"], 2);
    a.join().unwrap();

    let f = m.finish();
    assert_eq!(f.result.status, Status::Completed);
    assert_eq!(f.result.players[1].invalid_actions, 2);
    assert_eq!(f.result.players[1].outcome, Outcome::InvalidOutput);
    assert!(!f.logs.contains("panicked"), "{}", f.logs);
}

#[test]
fn illegal_actions_count_and_eventually_forfeit() {
    let m = Match::start("coin", json!({"max_invalid": 2}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    for turn in 1..=2u64 {
        assert_eq!(b.expect("turn")["turn"], turn);
        b.action(turn, json!(["not", "an", "object"]));
    }
    assert_eq!(a.join().unwrap().unwrap()["rank"], 1);
    let f = m.finish();
    assert_eq!(f.result.players[1].invalid_actions, 2);
    assert_eq!(f.result.players[1].outcome, Outcome::InvalidOutput);
}

#[test]
fn repeated_timeouts_forfeit() {
    let m = Match::start("coin", json!({"max_timeouts": 2}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    assert_eq!(b.expect("turn")["turn"], 1);
    assert_eq!(b.expect("turn")["turn"], 2); // proves turn 1 timed out
    assert_eq!(b.expect("end")["rank"], 2); // turn 2 timed out too: forfeit, match over
    assert_eq!(a.join().unwrap().unwrap()["rank"], 1);
    let f = m.finish();
    assert_eq!(
        (f.result.players[1].timeouts, f.result.players[1].outcome),
        (2, Outcome::Timeout)
    );
}

#[test]
fn a_bot_that_never_connects() {
    let m = Match::start("coin", json!({"connect_ms": 1_000}));
    let mut a = m.bot(0);
    a.join();
    assert_eq!(a.expect("end")["rank"], 1);
    let f = m.finish();
    assert_eq!(f.code, 0);
    assert_eq!(f.result.players[1].outcome, Outcome::NeverConnected);
}

#[test]
fn a_bot_that_crashes_mid_match_is_disconnected_and_the_other_wins() {
    let m = Match::start("coin", json!({"max_turns": 10}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    for turn in 1..=2u64 {
        assert_eq!(b.expect("turn")["turn"], turn);
        b.action(turn, json!({"move": "STAY"}));
    }
    drop(b);
    // Only the result is asserted: the controller may send one more turn before it notices.
    assert_eq!(a.join().unwrap().unwrap()["rank"], 1);
    let f = m.finish();
    assert_eq!(f.result.status, Status::Completed);
    assert_eq!(f.result.players[1].outcome, Outcome::Disconnected);
}

#[test]
fn a_bot_that_stops_reading_is_dropped_without_stalling_the_match() {
    let m = Match::start("big-state", json!({}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join(); // ...and then never reads its 20 MB states
    let started = Instant::now();
    assert_eq!(a.join().unwrap().unwrap()["rank"], 1);
    let f = m.finish();
    assert_eq!(f.result.players[1].outcome, Outcome::Disconnected);
    assert!(
        started.elapsed() < Duration::from_secs(15),
        "took {:?}",
        started.elapsed()
    );
    drop(b);
}

#[test]
fn sigterm_aborts_the_match_and_still_writes_the_output() {
    let m = Match::start("coin", json!({"max_turns": 50}));
    let (mut a, mut b) = (m.bot(0), m.bot(1));
    a.join();
    b.join();
    for bot in [&mut a, &mut b] {
        assert_eq!(bot.expect("turn")["turn"], 1);
        bot.action(1, json!({"move": "STAY"}));
    }
    for bot in [&mut a, &mut b] {
        assert_eq!(bot.expect("turn")["turn"], 2); // turn 1 is in the replay
    }
    m.terminate();
    let f = m.finish();
    assert_eq!(
        (f.code, f.result.status, f.result.turns),
        (1, Status::Aborted, 1)
    );
    assert!(f.result.players.iter().all(|p| p.rank.is_none()));
}

#[test]
fn a_panic_in_validate_is_an_invalid_action_and_is_logged_once() {
    let m = Match::start("panic-validate", json!({"max_turns": 3}));
    let a = autoplay(m.bot(0), stay);
    let mut b = m.bot(1);
    b.join();
    for turn in 1..=3u64 {
        assert_eq!(b.expect("turn")["turn"], turn);
        b.action(turn, json!({"move": "BOOM"}));
    }
    b.expect("end");
    a.join().unwrap();
    let f = m.finish();
    assert_eq!(f.result.status, Status::Completed);
    assert_eq!(
        (
            f.result.players[1].invalid_actions,
            f.result.players[1].outcome
        ),
        (3, Outcome::Ok)
    );
    assert_eq!(f.logs.matches("validate panicked").count(), 1, "{}", f.logs);
}

#[test]
fn a_panic_in_apply_voids_the_match() {
    let m = Match::start("crash-on-turn-2", json!({}));
    let (mut a, mut b) = (m.bot(0), m.bot(1));
    a.join();
    b.join();
    for turn in 1..=2u64 {
        for bot in [&mut a, &mut b] {
            assert_eq!(bot.expect("turn")["turn"], turn);
            bot.action(turn, json!({"move": "STAY"}));
        }
    }
    // No "end": the match is void.
    assert!(a.is_closed_by_server(CLOSE_WAIT) && b.is_closed_by_server(CLOSE_WAIT));
    let f = m.finish();
    assert_eq!((f.code, f.result.status), (1, Status::ControllerError));
}

type Mutation = fn(&mut Value);

#[test]
fn a_bad_spec_exits_with_code_2_names_the_problem_and_logs_no_token() {
    let base = json!({"match_id": "m", "attempt": 1, "seed": 1,
                      "players": [{"slot": 0, "token": "secret-token-a"}, {"slot": 1, "token": "secret-token-b"}]});
    let cases: Vec<(Mutation, &str)> = vec![
        (
            |s| drop(s.as_object_mut().unwrap().remove("seed")),
            "`seed`",
        ),
        (|s| s["players"][1]["slot"] = json!(0), "used twice"),
        (|s| s["limits"] = json!({"turn_ms": 0}), "`limits.turn_ms`"),
        (|s| s["surprise"] = json!(true), "unknown field `surprise`"),
        (|s| s["game"] = json!({"coins": 0}), "`coins`"), // rejected by the game's setup
    ];
    for (mutate, expected) in cases {
        let mut spec = base.clone();
        mutate(&mut spec);
        let (code, logs) = run_to_exit("coin", &spec.to_string());
        assert_eq!(code, 2, "{logs}");
        assert!(
            logs.contains(expected),
            "{logs:?} should mention {expected:?}"
        );
        assert!(!logs.contains("secret-token"), "a token was logged: {logs}");
    }
    let (code, logs) = run_to_exit("coin", r#"{"players": [{"token": "secret-token-a""#);
    assert_eq!(code, 2);
    assert!(
        logs.contains("not valid JSON") && !logs.contains("secret-token"),
        "{logs}"
    );
}
