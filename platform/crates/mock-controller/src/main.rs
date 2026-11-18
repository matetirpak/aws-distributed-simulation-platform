//! The game controller with a toy game, chosen by `MOCK_GAME` (see `games::NAMES`).
//! A real game's binary is the same one line: `controller::run::<TheGame>()`.

use std::process::ExitCode;

use games::{Coin, Nim, testing};

fn main() -> ExitCode {
    let game = std::env::var("MOCK_GAME").unwrap_or_default();
    match game.as_str() {
        "coin" => controller::run::<Coin>(),
        "nim" => controller::run::<Nim>(),
        "crash-on-turn-2" => controller::run::<testing::CrashOnTurn2>(),
        "panic-validate" => controller::run::<testing::PanicValidate>(),
        "big-state" => controller::run::<testing::BigState>(),
        other => {
            let msg = serde_json::json!({"level": "ERROR", "message": "unknown MOCK_GAME", "mock_game": other, "known": games::NAMES});
            println!("{msg}");
            ExitCode::from(2)
        }
    }
}
