use protocol::Replay;

use crate::{Game, game_rng};

/// Re-simulates a match from its replay and checks that it ends with the same scores.
/// Run it in a different process than the one that played: that also catches games whose
/// outcome depends on `HashMap` order, which changes from process to process.
pub fn verify<G: Game>(replay: &Replay) -> Result<String, String> {
    let mut game = G::setup(
        replay.num_players,
        game_rng(replay.seed),
        &replay.game_config,
    )
    .map_err(|e| format!("setup failed: {}", e.0))?;
    for t in &replay.turns {
        let movers = game.players_to_move();
        if movers != t.movers {
            return Err(format!(
                "turn {}: players to move {movers:?}, the replay says {:?}",
                t.turn, t.movers
            ));
        }
        let mut actions = Vec::with_capacity(movers.len());
        for &m in &movers {
            let action = match t.actions.get(m).and_then(Option::as_ref) {
                Some(value) => Some(game.validate(m, value).ok_or_else(|| {
                    format!("turn {}: slot {m}'s recorded action is not valid", t.turn)
                })?),
                None => None,
            };
            actions.push((m, action));
        }
        game.apply(&actions);
    }
    let scores = game.scores();
    match &replay.final_scores {
        Some(recorded) if *recorded != scores => Err(format!(
            "final scores {scores:?}, the replay says {recorded:?}"
        )),
        _ => Ok(format!(
            "{} turns, final scores {scores:?}",
            replay.turns.len()
        )),
    }
}
