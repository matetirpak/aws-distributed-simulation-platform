//! The contract between the game controller, the relay and the dispatcher.
//!
//! The source of truth is `docs/contracts.md`.
//! Field names here are binding: changing a message means bumping [`PROTOCOL_VERSION`] and
//! updating the controller, the relay and the starter kits in the same pull request.

mod spec;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub use spec::{Limits, MatchSpec, PlayerSpec, SpecError};

pub const PROTOCOL_VERSION: u32 = 1;
/// Longest line the controller accepts from a relay, without the newline.
pub const MAX_LINE: usize = 64 * 1024;
pub const DEFAULT_PORT: u16 = 9000;

/// Relay → controller. One minified JSON object per line.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum FromBot {
    Hello {
        token: String,
        protocol: u32,
    },
    Ready,
    /// `action` holds the bot's answer; `invalid` marks output the relay couldn't parse.
    Action {
        turn: u32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        action: Option<Value>,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        invalid: bool,
    },
}

/// Controller → relay. The relay passes these lines to the bot unchanged.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ToBot {
    Init {
        slot: usize,
        players: usize,
        config: Value,
        budget_ms: u64,
    },
    Turn {
        turn: u32,
        state: Value,
        budget_ms: u64,
    },
    End {
        rank: u32,
        score: i64,
    },
}

/// The fields the relay needs from a [`ToBot`] line, without parsing the (possibly large) state.
#[derive(Debug, Deserialize)]
pub struct ToBotHeader {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub turn: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Completed,
    Aborted,
    ControllerError,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Ok,
    Timeout,
    Disconnected,
    InvalidOutput,
    NeverConnected,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlayerResult {
    pub slot: usize,
    pub rank: Option<u32>,
    pub score: Option<i64>,
    pub outcome: Outcome,
    pub timeouts: u32,
    pub invalid_actions: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MatchResult {
    pub match_id: String,
    pub attempt: u32,
    pub status: Status,
    pub turns: u32,
    pub controller_version: String,
    pub game_version: Option<String>,
    pub players: Vec<PlayerResult>,
}

/// How long a mover took this turn: milliseconds, or `"timeout"`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TurnTime {
    Timeout,
    #[serde(untagged)]
    Ms(f64),
}

/// One turn of a replay. `actions` and `times_ms` have one entry per slot: `null` for
/// players who didn't move, timed out, or sent nothing valid.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReplayTurn {
    pub turn: u32,
    pub movers: Vec<usize>,
    pub actions: Vec<Option<Value>>,
    pub times_ms: Vec<Option<TurnTime>>,
}

/// Everything needed to re-simulate a match. Never contains tokens.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Replay {
    pub match_id: String,
    pub attempt: u32,
    pub seed: u32,
    pub num_players: usize,
    pub game_config: Value,
    pub limits: Limits,
    pub controller_version: String,
    pub game_version: Option<String>,
    pub turns: Vec<ReplayTurn>,
    pub final_scores: Option<Vec<i64>>,
    pub result: Option<MatchResult>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn messages_use_the_documented_wire_format() {
        let hello: FromBot =
            serde_json::from_str(r#"{"type":"hello","token":"t","protocol":1}"#).unwrap();
        assert_eq!(
            hello,
            FromBot::Hello {
                token: "t".into(),
                protocol: 1
            }
        );
        let invalid = FromBot::Action {
            turn: 3,
            action: None,
            invalid: true,
        };
        assert_eq!(
            serde_json::to_value(&invalid).unwrap(),
            json!({"type": "action", "turn": 3, "invalid": true})
        );
        let end = ToBot::End { rank: 1, score: 84 };
        assert_eq!(
            serde_json::to_value(&end).unwrap(),
            json!({"type": "end", "rank": 1, "score": 84})
        );
    }

    #[test]
    fn a_token_that_is_not_a_string_is_not_a_hello() {
        assert!(
            serde_json::from_str::<FromBot>(r#"{"type":"hello","token":["x"],"protocol":1}"#)
                .is_err()
        );
    }

    #[test]
    fn turn_times_are_numbers_or_timeout() {
        let times = vec![Some(TurnTime::Ms(12.5)), Some(TurnTime::Timeout), None];
        let text = serde_json::to_string(&times).unwrap();
        assert_eq!(text, r#"[12.5,"timeout",null]"#);
        assert_eq!(
            serde_json::from_str::<Vec<Option<TurnTime>>>(&text).unwrap(),
            times
        );
    }
}
