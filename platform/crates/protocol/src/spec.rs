//! The match spec (`MATCH_SPEC`), and its strict validation.
//!
//! Validation errors name the field but never echo a value, so a token can't leak into logs.

use std::collections::HashSet;
use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MatchSpec {
    pub match_id: String,
    pub attempt: u32,
    pub seed: u32,
    pub players: Vec<PlayerSpec>,
    #[serde(default)]
    pub limits: Limits,
    #[serde(default = "empty_object")]
    pub game: Value,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlayerSpec {
    pub slot: usize,
    pub token: String,
}

/// All time values in milliseconds. Missing fields take the defaults below.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub connect_ms: u64,
    pub init_ms: u64,
    pub turn_ms: u64,
    pub grace_ms: u64,
    pub max_turns: u32,
    pub total_ms: u64,
    pub max_timeouts: u32,
    pub max_invalid: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            connect_ms: 300_000, // covers a cold start of the bot machines
            init_ms: 5_000,
            turn_ms: 2_000,
            grace_ms: 100,
            max_turns: 500,
            total_ms: 900_000,
            max_timeouts: 3,
            max_invalid: 10,
        }
    }
}

const LIMIT_KEYS: [&str; 8] = [
    "connect_ms",
    "init_ms",
    "turn_ms",
    "grace_ms",
    "max_turns",
    "total_ms",
    "max_timeouts",
    "max_invalid",
];
const U32_LIMITS: [&str; 3] = ["max_turns", "max_timeouts", "max_invalid"];
const SPEC_KEYS: [&str; 6] = ["match_id", "attempt", "seed", "players", "limits", "game"];

fn empty_object() -> Value {
    Value::Object(Map::new())
}

// Debug output never shows tokens.
impl fmt::Debug for PlayerSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PlayerSpec")
            .field("slot", &self.slot)
            .field("token", &"<redacted>")
            .finish()
    }
}

impl fmt::Debug for MatchSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("MatchSpec")
            .field("match_id", &self.match_id)
            .field("attempt", &self.attempt)
            .field("seed", &self.seed)
            .field("players", &self.players)
            .field("limits", &self.limits)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpecError(pub String);

impl fmt::Display for SpecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SpecError {}

fn err<T>(msg: impl Into<String>) -> Result<T, SpecError> {
    Err(SpecError(msg.into()))
}

impl MatchSpec {
    /// Parses and validates a match spec. Every rule in the contract is checked here,
    /// before the controller listens: a bad spec is a configuration error (exit code 2).
    pub fn parse(text: &str) -> Result<MatchSpec, SpecError> {
        let root: Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => {
                return err(format!(
                    "match spec is not valid JSON (line {}, column {})",
                    e.line(),
                    e.column()
                ));
            }
        };
        let Some(obj) = root.as_object() else {
            return err("match spec must be a JSON object");
        };
        if let Some(key) = obj.keys().find(|k| !SPEC_KEYS.contains(&k.as_str())) {
            return err(format!("unknown field `{key}`"));
        }

        match obj.get("match_id") {
            Some(Value::String(s)) if !s.is_empty() && s.len() <= 128 => {}
            _ => return err("`match_id` must be a non-empty string of at most 128 characters"),
        }
        match obj.get("attempt").and_then(Value::as_u64) {
            Some(a) if a >= 1 && a <= u64::from(u32::MAX) => {}
            _ => return err("`attempt` must be an integer of at least 1"),
        }
        match obj.get("seed").and_then(Value::as_u64) {
            Some(s) if s <= u64::from(u32::MAX) => {}
            _ => return err("`seed` must be an integer from 0 to 4294967295"),
        }
        check_players(obj.get("players"))?;
        if let Some(limits) = obj.get("limits") {
            check_limits(limits)?;
        }
        if obj.get("game").is_some_and(|g| !g.is_object()) {
            return err("`game` must be a JSON object");
        }

        // Everything above passed, so this can only fail on a bug in the checks.
        serde_json::from_value(root)
            .map_err(|_| SpecError("match spec has an invalid structure".into()))
    }
}

fn check_players(players: Option<&Value>) -> Result<(), SpecError> {
    let Some(Value::Array(players)) = players else {
        return err("`players` must be an array");
    };
    if players.is_empty() {
        return err("`players` must not be empty");
    }
    let mut slots = HashSet::new();
    let mut tokens = HashSet::new();
    for (i, p) in players.iter().enumerate() {
        let Some(p) = p.as_object() else {
            return err(format!("`players[{i}]` must be an object"));
        };
        if let Some(key) = p.keys().find(|k| *k != "slot" && *k != "token") {
            return err(format!("unknown field `players[{i}].{key}`"));
        }
        let Some(slot) = p.get("slot").and_then(Value::as_u64) else {
            return err(format!(
                "`players[{i}].slot` must be a non-negative integer"
            ));
        };
        if !slots.insert(slot) {
            return err(format!("`players[{i}].slot` is used twice"));
        }
        let Some(token) = p
            .get("token")
            .and_then(Value::as_str)
            .filter(|t| !t.is_empty())
        else {
            return err(format!("`players[{i}].token` must be a non-empty string"));
        };
        if !tokens.insert(token) {
            return err(format!("`players[{i}].token` is used twice"));
        }
    }
    let n = players.len() as u64;
    if (0..n).any(|s| !slots.contains(&s)) {
        return err("`players[].slot` must be exactly 0..n-1");
    }
    Ok(())
}

fn check_limits(limits: &Value) -> Result<(), SpecError> {
    let Some(limits) = limits.as_object() else {
        return err("`limits` must be a JSON object");
    };
    for (key, value) in limits {
        if !LIMIT_KEYS.contains(&key.as_str()) {
            return err(format!("unknown field `limits.{key}`"));
        }
        let max = if U32_LIMITS.contains(&key.as_str()) {
            u64::from(u32::MAX)
        } else {
            u64::MAX
        };
        match value.as_u64() {
            Some(v) if v >= 1 && v <= max => {}
            _ => return err(format!("`limits.{key}` must be a positive integer")),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn valid() -> Value {
        json!({"match_id": "m-1", "attempt": 1, "seed": 7,
               "players": [{"slot": 1, "token": "secret-b"}, {"slot": 0, "token": "secret-a"}],
               "limits": {"turn_ms": 300}, "game": {"map": "x"}})
    }

    fn error_for(mutate: impl FnOnce(&mut Value)) -> String {
        let mut spec = valid();
        mutate(&mut spec);
        MatchSpec::parse(&spec.to_string()).unwrap_err().0
    }

    #[test]
    fn a_valid_spec_parses_and_merges_limits_over_defaults() {
        let spec = MatchSpec::parse(&valid().to_string()).unwrap();
        assert_eq!(spec.limits.turn_ms, 300);
        assert_eq!(spec.limits.init_ms, Limits::default().init_ms);
        assert_eq!(spec.players.len(), 2);
    }

    type Mutation = Box<dyn FnOnce(&mut Value)>;

    #[test]
    fn errors_name_the_field_and_never_show_a_token() {
        let cases: Vec<(Mutation, &str)> = vec![
            (
                Box::new(|s| {
                    s.as_object_mut().unwrap().remove("seed");
                }),
                "`seed`",
            ),
            (Box::new(|s| s["seed"] = json!(-1)), "`seed`"),
            (Box::new(|s| s["attempt"] = json!(0)), "`attempt`"),
            (Box::new(|s| s["match_id"] = json!("")), "`match_id`"),
            (Box::new(|s| s["extra"] = json!(1)), "unknown field `extra`"),
            (Box::new(|s| s["players"] = json!([])), "`players`"),
            (
                Box::new(|s| s["players"][1]["slot"] = json!(1)),
                "used twice",
            ),
            (Box::new(|s| s["players"][1]["slot"] = json!(5)), "0..n-1"),
            (
                Box::new(|s| s["players"][0]["token"] = json!("")),
                "`players[0].token`",
            ),
            (
                Box::new(|s| s["players"][0]["token"] = json!("secret-a")),
                "used twice",
            ),
            (
                Box::new(|s| s["players"][0]["token"] = json!(42)),
                "`players[0].token`",
            ),
            (
                Box::new(|s| s["limits"]["turn_ms"] = json!(0)),
                "`limits.turn_ms`",
            ),
            (
                Box::new(|s| s["limits"]["warp"] = json!(1)),
                "unknown field `limits.warp`",
            ),
            (
                Box::new(|s| s["limits"]["max_turns"] = json!(5_000_000_000u64)),
                "`limits.max_turns`",
            ),
            (Box::new(|s| s["game"] = json!([1])), "`game`"),
        ];
        for (mutate, expected) in cases {
            let msg = error_for(mutate);
            assert!(
                msg.contains(expected),
                "{msg:?} should mention {expected:?}"
            );
            assert!(!msg.contains("secret"), "{msg:?} leaks a token");
        }
    }

    #[test]
    fn invalid_json_reports_only_the_position() {
        let msg = MatchSpec::parse(r#"{"players": [{"token": "secret-a""#)
            .unwrap_err()
            .0;
        assert!(
            msg.starts_with("match spec is not valid JSON") && !msg.contains("secret"),
            "{msg}"
        );
    }

    #[test]
    fn debug_output_redacts_tokens() {
        let spec = MatchSpec::parse(&valid().to_string()).unwrap();
        assert!(!format!("{spec:?}").contains("secret"));
    }
}
