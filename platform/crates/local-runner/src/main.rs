//! Plays one match locally with docker compose, the way the dispatcher will on AWS: fresh
//! tokens and seed, a match spec, three isolated containers, then the result and a replay check.
//!
//! ```text
//! cargo run -p local-runner -- coin ok ok
//! cargo run -p local-runner -- nim ok slow
//! cargo run -p local-runner -- coin ok absent     # the second bot never starts
//! cargo run -p local-runner -- --controller-image game-controller:v0.1.0 ok ok
//! ```
//!
//! `--controller-image` plays a smoke match with a real game's controller image instead of our
//! mock. It passes when the controller completes the match; our bots don't know that game, so
//! their outcomes don't matter, and its replay isn't re-simulated here.
//!
//! Bot modes are described in `bots/python/bot.py`. Output goes to `out/<match_id>/`.

use std::fs::{self, File};
use std::io::Read;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use protocol::{Limits, MatchResult, MatchSpec, PlayerSpec, Status};
use serde_json::{Value, json};

const GAMES: [&str; 2] = ["coin", "nim"];
const MODES: [&str; 8] = [
    "ok", "slow", "crash", "garbage", "huge", "silent", "noflush", "probe",
];
/// Shown instead of a game name for an external controller image.
const EXTERNAL: &str = "external";

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut image = None;
    if let Some(i) = args.iter().position(|a| a == "--controller-image") {
        if i + 1 >= args.len() {
            return usage();
        }
        image = Some(args.remove(i + 1));
        args.remove(i);
    }
    let (game, mode0, mode1) = match (&image, args.as_slice()) {
        (None, [game, mode0, mode1]) if GAMES.contains(&game.as_str()) => {
            (game.as_str(), mode0, mode1)
        }
        (Some(_), [mode0, mode1]) => (EXTERNAL, mode0, mode1),
        _ => return usage(),
    };
    let known = |m: &str| MODES.contains(&m);
    if !known(mode0) || !(known(mode1) || mode1 == "absent") {
        return usage();
    }
    match run(game, mode0, mode1, image.as_deref()) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(2)
        }
    }
}

fn usage() -> ExitCode {
    eprintln!(
        "usage: local-runner <{}> <mode> <mode|absent>",
        GAMES.join("|")
    );
    eprintln!("       local-runner --controller-image <image> <mode> <mode|absent>");
    eprintln!("modes: {}", MODES.join(" "));
    ExitCode::from(2)
}

fn run(game: &str, mode0: &str, mode1: &str, image: Option<&str>) -> Result<bool, String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let id = format!("{}-{}", unix_time(), hex(&random::<3>()));
    let tokens = [hex(&random::<16>()), hex(&random::<16>())];
    let spec = MatchSpec {
        match_id: format!("local-{id}"),
        attempt: 1,
        seed: u32::from_le_bytes(random::<4>()),
        players: (0..2)
            .map(|slot| PlayerSpec {
                slot,
                token: tokens[slot].clone(),
            })
            .collect(),
        // Locally the bots start in seconds; AWS keeps the 5-minute default.
        limits: Limits {
            connect_ms: 30_000,
            ..Limits::default()
        },
        game: json!({}),
    };
    let out_dir = root.join("out").join(&spec.match_id);
    fs::create_dir_all(&out_dir)
        .map_err(|e| format!("cannot create {}: {e}", out_dir.display()))?;
    let owner = fs::metadata(&out_dir).map_err(|e| e.to_string())?;

    let spec_json = serde_json::to_string(&spec).map_err(|e| e.to_string())?;
    // Passed in the environment, never on the command line: the spec holds the tokens.
    let mut env = vec![
        ("MATCH_SPEC", spec_json),
        // A real game's image ignores MOCK_GAME; our mock image, run as "external" to test this
        // path, then plays coin.
        (
            "MOCK_GAME",
            if image.is_some() { "coin" } else { game }.into(),
        ),
        ("TOKEN0", tokens[0].clone()),
        ("TOKEN1", tokens[1].clone()),
        ("MODE0", mode0.into()),
        ("MODE1", mode1.into()),
        ("OUT_DIR", out_dir.display().to_string()),
        ("HOST_UID", owner.uid().to_string()),
        ("HOST_GID", owner.gid().to_string()),
    ];
    if let Some(image) = image {
        env.push(("CONTROLLER_IMAGE", image.into()));
    }
    let compose = Compose {
        root: root.clone(),
        project: format!("pg-{id}"),
        env,
    };
    let mut services = vec!["controller", "bot0"];
    if mode1 != "absent" {
        services.push("bot1");
    }

    println!(
        "match {} ({game}): slot 0 = {mode0}, slot 1 = {mode1}",
        spec.match_id
    );
    if image.is_some() {
        // Build only the bot image; the controller image is used as given.
        compose.ok(&["build", "--quiet", "bot0"], &[])?;
        compose.ok(&["up", "--no-build", "--detach", "--quiet-pull"], &services)?;
    } else {
        compose.ok(
            &["up", "--build", "--detach", "--quiet-build", "--quiet-pull"],
            &services,
        )?;
    }
    // `compose wait` exits with the container's exit code.
    let controller_exit = compose
        .command(&["wait", "controller"], &[])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .ok()
        .and_then(|s| s.code())
        .unwrap_or(-1);
    // The relays stop their bots within about 2 s of the controller closing; wait for their reports.
    let _ = compose
        .command(&["wait"], &services[1..])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let bot_logs: Vec<String> = services[1..]
        .iter()
        .map(|bot| {
            compose
                .output(&["logs", "--no-color", "--no-log-prefix", bot])
                .unwrap_or_default()
        })
        .collect();
    let controller_logs = compose.output(&["logs", "--no-color", "--no-log-prefix", "controller"]);
    drop(compose); // `docker compose down`

    println!("\ncontroller exit code: {controller_exit}");
    if controller_exit != 0 {
        println!("controller logs:\n{}", controller_logs.unwrap_or_default());
    }
    for (bot, logs) in services[1..].iter().zip(&bot_logs) {
        print_relay_report(bot, logs);
    }

    let result = read_json(&out_dir.join("result.json"))?;
    let modes = [mode0, mode1];
    println!("\nstatus {:?}, {} turns", result.status, result.turns);
    println!(
        "{:<5} {:<8} {:<16} {:>4} {:>6} {:>9} {:>8}",
        "slot", "bot", "outcome", "rank", "score", "timeouts", "invalid"
    );
    for p in &result.players {
        let show = |v: Option<String>| v.unwrap_or_else(|| "-".into());
        println!(
            "{:<5} {:<8} {:<16} {:>4} {:>6} {:>9} {:>8}",
            p.slot,
            modes[p.slot],
            format!("{:?}", p.outcome),
            show(p.rank.map(|r| r.to_string())),
            show(p.score.map(|s| s.to_string())),
            p.timeouts,
            p.invalid_actions
        );
    }

    let replay =
        controller::read_replay(&out_dir.join("replay.json.gz")).map_err(|e| e.to_string())?;
    let verified = if image.is_some() {
        // We don't have that game's rules; it re-simulates its replays in its own tests.
        println!("\nreplay re-simulation skipped: external controller image");
        Ok(String::new())
    } else {
        let verified = games::verify(game, &replay);
        match &verified {
            Ok(detail) => println!("\nreplay re-simulated: OK ({detail})"),
            Err(detail) => println!("\nreplay re-simulated: MISMATCH ({detail})"),
        }
        verified
    };
    println!("output: {}", out_dir.display());
    Ok(controller_exit == 0 && result.status == Status::Completed && verified.is_ok())
}

/// Shows what the relay reported about its bot: exit status and captured stderr.
fn print_relay_report(bot: &str, logs: &str) {
    let report = logs
        .lines()
        .rev()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .find(|v| v.get("relay").is_some());
    let Some(report) = report else {
        println!("{bot}: no relay report");
        return;
    };
    println!(
        "{bot}: {} ({}), stderr {} bytes",
        report["relay"],
        report["bot_exit"].as_str().unwrap_or("?"),
        report["stderr_bytes"]
    );
    for line in report["stderr"].as_str().unwrap_or("").lines().take(5) {
        println!("  | {line}");
    }
}

/// One compose project per match; `down` runs when it goes out of scope, even on errors.
struct Compose {
    root: PathBuf,
    project: String,
    env: Vec<(&'static str, String)>,
}

impl Compose {
    fn command(&self, args: &[&str], services: &[&str]) -> Command {
        let mut cmd = Command::new("docker");
        cmd.current_dir(&self.root)
            .args(["compose", "--project-name", &self.project])
            .args(args)
            .args(services)
            .envs(self.env.iter().map(|(k, v)| (*k, v.as_str())));
        cmd
    }

    fn ok(&self, args: &[&str], services: &[&str]) -> Result<(), String> {
        let status = self
            .command(args, services)
            .status()
            .map_err(|e| format!("docker: {e}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!("docker compose {} failed", args[0]))
        }
    }

    fn output(&self, args: &[&str]) -> Result<String, String> {
        let out = self
            .command(args, &[])
            .stderr(Stdio::inherit())
            .output()
            .map_err(|e| e.to_string())?;
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }
}

impl Drop for Compose {
    fn drop(&mut self) {
        let _ = self
            .command(
                &["down", "--volumes", "--remove-orphans", "--timeout", "2"],
                &[],
            )
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

fn read_json(path: &Path) -> Result<MatchResult, String> {
    let text =
        fs::read_to_string(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))
}

fn random<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .expect("read /dev/urandom");
    bytes
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unix_time() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}
