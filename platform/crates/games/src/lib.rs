//! Toy games that stand in for a real game's crate. They implement the
//! same `Game` trait a real game will, so the controller, relay and images can be tested
//! end to end with both turn models.

mod coin;
mod nim;
pub mod testing;

pub use coin::{Coin, Move};
pub use nim::{Nim, Take};

use protocol::Replay;

/// Names accepted by `MOCK_GAME` and the local runner.
pub const NAMES: [&str; 5] = [
    "coin",
    "nim",
    "crash-on-turn-2",
    "panic-validate",
    "big-state",
];

/// Re-simulates a replay of the game called `name` (see [`NAMES`]).
pub fn verify(name: &str, replay: &Replay) -> Result<String, String> {
    match name {
        "coin" => controller::verify::<Coin>(replay),
        "nim" => controller::verify::<Nim>(replay),
        "crash-on-turn-2" => controller::verify::<testing::CrashOnTurn2>(replay),
        "panic-validate" => controller::verify::<testing::PanicValidate>(replay),
        "big-state" => controller::verify::<testing::BigState>(replay),
        other => Err(format!("unknown game {other:?}")),
    }
}
