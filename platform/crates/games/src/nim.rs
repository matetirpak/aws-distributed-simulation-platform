//! Nim with alternating turns: take at least one object from one heap; whoever takes the
//! last object wins. A player with no valid action passes.

use controller::{Game, GameRng, RngCore, SetupError};
use serde_json::{Value, json};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Take {
    pub heap: usize,
    pub count: u64,
}

pub struct Nim {
    initial: Vec<u64>,
    heaps: Vec<u64>,
    to_move: usize,
    winner: Option<usize>,
}

impl Game for Nim {
    type Action = Take;

    fn setup(num_players: usize, mut rng: GameRng, config: &Value) -> Result<Self, SetupError> {
        if num_players != 2 {
            return Err(SetupError("Nim needs exactly 2 players".into()));
        }
        let heaps: Vec<u64> = match config.get("heaps") {
            None => (0..3).map(|_| 1 + rng.next_u64() % 7).collect(),
            Some(v) => v
                .as_array()
                .filter(|a| (1..=10).contains(&a.len()))
                .and_then(|a| {
                    a.iter()
                        .map(|h| h.as_u64().filter(|h| (1..=1000).contains(h)))
                        .collect()
                })
                .ok_or_else(|| SetupError("`heaps` must be 1-10 integers from 1 to 1000".into()))?,
        };
        Ok(Nim {
            initial: heaps.clone(),
            heaps,
            to_move: 0,
            winner: None,
        })
    }

    fn init_info(&self, _player: usize) -> Value {
        json!({"heaps": self.initial, "rules": "take at least 1 from one heap; whoever takes the last object wins"})
    }

    fn state(&self, _player: usize) -> Value {
        json!({"heaps": self.heaps, "to_move": self.to_move})
    }

    fn players_to_move(&self) -> Vec<usize> {
        vec![self.to_move]
    }

    fn validate(&self, player: usize, action: &Value) -> Option<Take> {
        let heap = usize::try_from(action.get("heap")?.as_u64()?).ok()?;
        let count = action.get("take")?.as_u64()?;
        let ok = player == self.to_move
            && count >= 1
            && self.heaps.get(heap).is_some_and(|&h| count <= h);
        ok.then_some(Take { heap, count })
    }

    fn apply(&mut self, actions: &[(usize, Option<Take>)]) {
        if let Some(&(player, Some(take))) = actions.first() {
            self.heaps[take.heap] -= take.count;
            if self.heaps.iter().all(|&h| h == 0) {
                self.winner = Some(player);
            }
        }
        self.to_move = 1 - self.to_move;
    }

    fn is_over(&self) -> bool {
        self.winner.is_some()
    }

    fn scores(&self) -> Vec<i64> {
        (0..2).map(|p| i64::from(self.winner == Some(p))).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use controller::game_rng;

    #[test]
    fn turns_alternate_and_the_last_taker_wins() {
        let mut nim = Nim::setup(2, game_rng(1), &json!({"heaps": [1, 2]})).unwrap();
        assert_eq!(nim.players_to_move(), vec![0]);
        let take = nim.validate(0, &json!({"heap": 1, "take": 2})).unwrap();
        nim.apply(&[(0, Some(take))]);
        assert_eq!(nim.players_to_move(), vec![1]);
        assert_eq!(
            nim.validate(0, &json!({"heap": 0, "take": 1})),
            None,
            "not player 0's turn"
        );
        let take = nim.validate(1, &json!({"heap": 0, "take": 1})).unwrap();
        nim.apply(&[(1, Some(take))]);
        assert!(nim.is_over());
        assert_eq!(nim.scores(), vec![0, 1]);
    }

    #[test]
    fn validate_rejects_junk_and_overdraws() {
        let nim = Nim::setup(2, game_rng(1), &json!({"heaps": [3]})).unwrap();
        for junk in [
            json!(null),
            json!({"heap": 0, "take": 4}),
            json!({"heap": 9, "take": 1}),
            json!({"heap": 0, "take": 0}),
            json!({"heap": -1, "take": 1}),
        ] {
            assert_eq!(nim.validate(0, &junk), None, "{junk}");
        }
    }
}
