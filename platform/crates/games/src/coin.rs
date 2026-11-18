//! Two players move on a grid at the same time and collect coins (simultaneous turns).

use std::collections::BTreeSet;

use controller::{Game, GameRng, RngCore, SetupError};
use serde_json::{Value, json};

const DEFAULT_BOARD: [&str; 8] = [
    "........", "..#..#..", "..#..#..", "........", "........", "..#..#..", "..#..#..", "........",
];
const DEFAULT_COINS: u64 = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Move {
    N,
    E,
    S,
    W,
    Stay,
}

impl Move {
    fn parse(s: &str) -> Option<Move> {
        Some(match s {
            "N" => Move::N,
            "E" => Move::E,
            "S" => Move::S,
            "W" => Move::W,
            "STAY" => Move::Stay,
            _ => return None,
        })
    }

    fn delta(self) -> (i64, i64) {
        match self {
            Move::N => (0, -1),
            Move::E => (1, 0),
            Move::S => (0, 1),
            Move::W => (-1, 0),
            Move::Stay => (0, 0),
        }
    }
}

pub struct Coin {
    board: Vec<String>,
    width: i64,
    height: i64,
    positions: [(i64, i64); 2],
    points: [i64; 2],
    // Ordered, so iteration never depends on hashing.
    coins: BTreeSet<(i64, i64)>,
}

impl Coin {
    fn walkable(&self, x: i64, y: i64) -> bool {
        (0..self.width).contains(&x)
            && (0..self.height).contains(&y)
            && self.board[y as usize].as_bytes()[x as usize] != b'#'
    }
}

fn parse_board(value: &Value) -> Result<Vec<String>, SetupError> {
    let bad = || {
        SetupError(
            "`board` must be 1-64 rows of equal length (1-64), using only '.' and '#'".into(),
        )
    };
    let rows = value.as_array().ok_or_else(bad)?;
    let board: Vec<String> = rows
        .iter()
        .map(|r| r.as_str().map(str::to_owned))
        .collect::<Option<_>>()
        .ok_or_else(bad)?;
    let width = board.first().map_or(0, String::len);
    let ok = (1..=64).contains(&board.len())
        && (1..=64).contains(&width)
        && board
            .iter()
            .all(|r| r.len() == width && r.bytes().all(|c| c == b'.' || c == b'#'));
    if ok { Ok(board) } else { Err(bad()) }
}

impl Game for Coin {
    type Action = Move;

    fn setup(num_players: usize, mut rng: GameRng, config: &Value) -> Result<Self, SetupError> {
        if num_players != 2 {
            return Err(SetupError("the coin game needs exactly 2 players".into()));
        }
        let board = match config.get("board") {
            Some(b) => parse_board(b)?,
            None => DEFAULT_BOARD.iter().map(|r| r.to_string()).collect(),
        };
        let (width, height) = (board[0].len() as i64, board.len() as i64);
        let starts = [(0, 0), (width - 1, height - 1)];
        let mut game = Coin {
            board,
            width,
            height,
            positions: starts,
            points: [0, 0],
            coins: BTreeSet::new(),
        };
        if !starts.iter().all(|&(x, y)| game.walkable(x, y)) {
            return Err(SetupError("both corners (start cells) must be '.'".into()));
        }

        // Free cells in row-major order, then a partial Fisher-Yates shuffle with the match RNG.
        let mut free: Vec<(i64, i64)> = (0..height)
            .flat_map(|y| (0..width).map(move |x| (x, y)))
            .filter(|&(x, y)| game.walkable(x, y) && !starts.contains(&(x, y)))
            .collect();
        let count = match config.get("coins") {
            None => DEFAULT_COINS.min(free.len() as u64) as usize,
            Some(c) => match c.as_u64() {
                Some(c) if c >= 1 && c <= free.len() as u64 => c as usize,
                _ => return Err(SetupError(format!("`coins` must be 1-{}", free.len()))),
            },
        };
        for i in 0..count {
            let j = i + (rng.next_u64() % (free.len() - i) as u64) as usize;
            free.swap(i, j);
        }
        game.coins = free[..count].iter().copied().collect();
        Ok(game)
    }

    fn init_info(&self, _player: usize) -> Value {
        json!({"width": self.width, "height": self.height, "board": self.board})
    }

    fn state(&self, _player: usize) -> Value {
        let cells =
            |it: &mut dyn Iterator<Item = (i64, i64)>| it.map(|(x, y)| [x, y]).collect::<Vec<_>>();
        json!({
            "positions": cells(&mut self.positions.iter().copied()),
            "coins": cells(&mut self.coins.iter().copied()),
            "scores": self.points,
        })
    }

    fn players_to_move(&self) -> Vec<usize> {
        vec![0, 1]
    }

    fn validate(&self, _player: usize, action: &Value) -> Option<Move> {
        action.get("move")?.as_str().and_then(Move::parse)
    }

    fn apply(&mut self, actions: &[(usize, Option<Move>)]) {
        for &(player, mv) in actions {
            let (dx, dy) = mv.unwrap_or(Move::Stay).delta(); // nothing valid: stay in place
            let (x, y) = (self.positions[player].0 + dx, self.positions[player].1 + dy);
            if self.walkable(x, y) {
                self.positions[player] = (x, y);
            }
        }
        for player in 0..2 {
            if self.coins.contains(&self.positions[player]) {
                self.points[player] += 1; // both score when they reach a coin together
            }
        }
        for pos in self.positions {
            self.coins.remove(&pos);
        }
    }

    fn is_over(&self) -> bool {
        self.coins.is_empty()
    }

    fn scores(&self) -> Vec<i64> {
        self.points.to_vec()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use controller::game_rng;

    #[test]
    fn same_seed_same_coins_different_seed_different_coins() {
        let coins = |seed| Coin::setup(2, game_rng(seed), &json!({})).unwrap().coins;
        assert_eq!(coins(7), coins(7));
        assert_ne!(coins(7), coins(8));
        assert_eq!(coins(7).len(), DEFAULT_COINS as usize);
    }

    #[test]
    fn validate_accepts_any_json_without_panicking() {
        let game = Coin::setup(2, game_rng(1), &json!({})).unwrap();
        for junk in [
            json!(null),
            json!([1, 2]),
            json!("N"),
            json!({"move": 5}),
            json!({"move": "TELEPORT"}),
        ] {
            assert_eq!(game.validate(0, &junk), None);
        }
        assert_eq!(game.validate(0, &json!({"move": "E"})), Some(Move::E));
    }

    #[test]
    fn walls_and_edges_block_moves() {
        let mut game =
            Coin::setup(2, game_rng(1), &json!({"board": ["..", "#."], "coins": 1})).unwrap();
        game.apply(&[(0, Some(Move::S)), (1, Some(Move::E))]);
        assert_eq!(game.positions, [(0, 0), (1, 1)]);
    }
}
