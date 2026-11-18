//! The game controller: hosts exactly one match, then exits.
//!
//! ```text
//! MATCH_SPEC -> game setup -> listen -> bots authenticate with tokens -> init
//!   -> turns with per-bot deadlines -> end -> replay.json.gz, result.json -> exit
//! ```
//!
//! The rules implemented here are listed in `docs/architecture.md`
//! ("Controller requirements"). A game only
//! implements [`Game`]; everything around the rules lives in this crate.
//!
//! Exit codes: 0 = completed, 1 = aborted or controller error, 2 = bad configuration.

mod game;
mod lobby;
mod output;
mod panics;
mod play;
mod player;
mod verify;

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use protocol::{DEFAULT_PORT, MatchSpec};
use tracing::error;

pub use game::{Game, GameRng, SetupError, game_rng};
pub use output::read_replay;
pub use rand_chacha::rand_core::RngCore;
pub use verify::verify;

pub const CONTROLLER_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Runs one match of `G`, configured by the environment:
///
/// | Variable | Meaning |
/// |---|---|
/// | `MATCH_SPEC` (or `MATCH_SPEC_FILE`) | The match spec, set by the dispatcher |
/// | `PORT` | Where bots connect (default 9000) |
/// | `OUT_DIR` | Where `replay.json.gz` and `result.json` go (default `/out`) |
/// | `GAME_VERSION` | Recorded in the result and the replay |
pub fn run<G: Game>() -> ExitCode {
    init_logging();
    panics::install_hook();

    let config = match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            error!(error = %e, "bad configuration");
            return ExitCode::from(2);
        }
    };
    let spec = &config.spec;
    let game =
        match panics::quietly(|| G::setup(spec.players.len(), game_rng(spec.seed), &spec.game)) {
            Ok(Ok(game)) => game,
            Ok(Err(e)) => {
                error!(error = %e.0, "the game rejected the match spec");
                return ExitCode::from(2);
            }
            Err(panic) => {
                error!(panic = %panic, "game setup panicked");
                return ExitCode::from(2);
            }
        };

    // Two workers: the readers keep timestamping arrivals while the match loop runs game code.
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            error!(error = %e, "cannot start the async runtime");
            return ExitCode::from(1);
        }
    };
    runtime.block_on(play::run_match(game, config))
}

pub(crate) struct Config {
    pub spec: MatchSpec,
    pub port: u16,
    pub out_dir: PathBuf,
    pub game_version: Option<String>,
}

impl Config {
    fn from_env() -> Result<Config, String> {
        let text = match (env::var("MATCH_SPEC"), env::var("MATCH_SPEC_FILE")) {
            (Ok(spec), _) => spec,
            (Err(_), Ok(path)) => std::fs::read_to_string(&path)
                .map_err(|e| format!("cannot read MATCH_SPEC_FILE: {e}"))?,
            _ => return Err("MATCH_SPEC or MATCH_SPEC_FILE must be set".into()),
        };
        let spec = MatchSpec::parse(&text).map_err(|e| e.0)?;
        let port = match env::var("PORT") {
            Ok(p) => p
                .parse()
                .map_err(|_| "PORT must be a port number".to_string())?,
            Err(_) => DEFAULT_PORT,
        };
        Ok(Config {
            spec,
            port,
            out_dir: env::var_os("OUT_DIR").map_or_else(|| PathBuf::from("/out"), PathBuf::from),
            game_version: env::var("GAME_VERSION").ok(),
        })
    }
}

/// JSON lines on stdout, for CloudWatch. Never log tokens or the full spec.
fn init_logging() {
    let _ = tracing_subscriber::fmt()
        .json()
        .flatten_event(true)
        .with_current_span(false)
        .with_span_list(false)
        .with_target(false)
        .with_max_level(tracing::Level::INFO)
        .with_writer(std::io::stdout)
        .try_init();
}
