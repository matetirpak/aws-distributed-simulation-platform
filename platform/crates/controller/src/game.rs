use rand_chacha::rand_core::SeedableRng;
use serde_json::Value;

/// The only randomness a game may use. ChaCha8 gives the same stream on every platform and
/// across versions, so a replay re-simulates identically.
pub type GameRng = rand_chacha::ChaCha8Rng;

pub fn game_rng(seed: u32) -> GameRng {
    GameRng::seed_from_u64(u64::from(seed))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetupError(pub String);

/// The rules of a game. Everything else (connections, tokens, time limits, forfeits,
/// results, replays) is the controller's job. Requirements for implementations:
/// `docs/contracts.md`.
///
/// - Use only the `GameRng` passed to `setup`; no clock, no I/O, no threads.
/// - Don't let `HashMap`/`HashSet` iteration order affect the outcome.
/// - `validate` must accept any JSON. A panic there counts as an invalid action;
///   a panic anywhere else voids the match.
pub trait Game: Sized {
    type Action;

    /// Builds the initial state. `config` is the match spec's `game` section.
    fn setup(num_players: usize, rng: GameRng, config: &Value) -> Result<Self, SetupError>;
    /// Static data, sent once in `init`.
    fn init_info(&self, player: usize) -> Value;
    /// Dynamic state, sent every turn, as `player` sees it.
    fn state(&self, player: usize) -> Value;
    /// Who moves this turn: everyone for simultaneous turns, one player for alternating turns.
    fn players_to_move(&self) -> Vec<usize>;
    /// Parses and checks a bot's action. `None` means invalid.
    fn validate(&self, player: usize, action: &Value) -> Option<Self::Action>;
    /// Advances one turn: one entry per mover, `None` when the bot sent nothing valid in time.
    fn apply(&mut self, actions: &[(usize, Option<Self::Action>)]);
    fn is_over(&self) -> bool;
    /// One score per player, higher is better.
    fn scores(&self) -> Vec<i64>;
}
