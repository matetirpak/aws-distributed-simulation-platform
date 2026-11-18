//! Deliberately broken games for the controller's protocol tests.

use controller::{Game, GameRng, SetupError};
use serde_json::{Value, json};

use crate::{Coin, Move};

/// `apply` panics on the second turn: the match must be voided (`controller_error`).
pub struct CrashOnTurn2 {
    inner: Coin,
    applied: u32,
}

/// `validate` panics on `{"move": "BOOM"}`: that must count as an invalid action, logged once.
pub struct PanicValidate {
    inner: Coin,
}

/// Sends slot 1 a 20 MB state every turn, to test bots that stop reading.
pub struct BigState {
    inner: Coin,
}

macro_rules! delegate_rest {
    () => {
        fn init_info(&self, player: usize) -> Value {
            self.inner.init_info(player)
        }
        fn players_to_move(&self) -> Vec<usize> {
            self.inner.players_to_move()
        }
        fn is_over(&self) -> bool {
            self.inner.is_over()
        }
        fn scores(&self) -> Vec<i64> {
            self.inner.scores()
        }
    };
}

impl Game for CrashOnTurn2 {
    type Action = Move;
    fn setup(n: usize, rng: GameRng, config: &Value) -> Result<Self, SetupError> {
        Ok(CrashOnTurn2 {
            inner: Coin::setup(n, rng, config)?,
            applied: 0,
        })
    }
    fn state(&self, player: usize) -> Value {
        self.inner.state(player)
    }
    fn validate(&self, player: usize, action: &Value) -> Option<Move> {
        self.inner.validate(player, action)
    }
    fn apply(&mut self, actions: &[(usize, Option<Move>)]) {
        self.applied += 1;
        assert!(self.applied != 2, "bug in the game logic");
        self.inner.apply(actions);
    }
    delegate_rest!();
}

impl Game for PanicValidate {
    type Action = Move;
    fn setup(n: usize, rng: GameRng, config: &Value) -> Result<Self, SetupError> {
        Ok(PanicValidate {
            inner: Coin::setup(n, rng, config)?,
        })
    }
    fn state(&self, player: usize) -> Value {
        self.inner.state(player)
    }
    fn validate(&self, player: usize, action: &Value) -> Option<Move> {
        assert!(
            action.get("move") != Some(&json!("BOOM")),
            "validate can't handle BOOM"
        );
        self.inner.validate(player, action)
    }
    fn apply(&mut self, actions: &[(usize, Option<Move>)]) {
        self.inner.apply(actions);
    }
    delegate_rest!();
}

impl Game for BigState {
    type Action = Move;
    fn setup(n: usize, rng: GameRng, config: &Value) -> Result<Self, SetupError> {
        Ok(BigState {
            inner: Coin::setup(n, rng, config)?,
        })
    }
    fn state(&self, player: usize) -> Value {
        let mut state = self.inner.state(player);
        if player == 1 {
            state["pad"] = json!("x".repeat(20_000_000));
        }
        state
    }
    fn validate(&self, player: usize, action: &Value) -> Option<Move> {
        self.inner.validate(player, action)
    }
    fn apply(&mut self, actions: &[(usize, Option<Move>)]) {
        self.inner.apply(actions);
    }
    delegate_rest!();
}
