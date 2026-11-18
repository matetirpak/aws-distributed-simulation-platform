"""The entrypoint of one match task on Fargate.

`play`   runs the controller here and starts each bot in a task of its own (Fargate or EC2), which
         dials this task's private address. Then it uploads the replay to S3 and writes the result
         to Postgres.
`report` prints the numbers of one batch as JSON, because the database has no public address.

The bots share nothing with this task: not a kernel, a filesystem or a network. Each one gets its
token through RunTask's overrides, so tokens are visible to anyone who may call DescribeTasks in
this account. Each is single-use and spent as soon as its bot connects.
"""

import hashlib
import json
import os
import random
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import bot_tasks

CONTROLLER = "/app/controller"
PORT = "9000"
# SCORE_FORMAT=win-points: the real game's controller ranks by win points, then active rovers, then
# rocks destroyed, encoded in one score. push-image.sh sets it when the controller image's
# GAME_VERSION is a commit hash; every other game's scores are stored as they are.
WIN_POINT_WEIGHT = 1_000_000_000
ACTIVE_ROVER_WEIGHT = 100_000
FULL_GAME_TURNS = 52_000
# connect_ms covers starting two Fargate tasks: about a minute, more when many start at once.
LIMITS = {"connect_ms": 300_000, "turn_ms": 2_000, "total_ms": 3_600_000}

SCHEMA = """
CREATE TABLE IF NOT EXISTS match_results (
    match_id      text PRIMARY KEY,
    batch_id      text NOT NULL,
    seed          bigint NOT NULL,
    strategy_a    text NOT NULL,
    strategy_b    text NOT NULL,
    status        text NOT NULL,
    winner        text,
    score_a       bigint,
    score_b       bigint,
    win_points_a  integer,
    win_points_b  integer,
    active_a      integer,
    active_b      integer,
    rocks_a       integer,
    rocks_b       integer,
    turns         integer,
    timeouts      integer,
    invalid       integer,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL,
    replay_key    text,
    image         text,
    game_version  text
)
"""

# Columns added after the first batches, for tables created before them.
MIGRATIONS = [
    "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS score_a bigint",
    "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS score_b bigint",
    "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS game_version text",
]

INSERT = """
INSERT INTO match_results (
    match_id, batch_id, seed, strategy_a, strategy_b, status, winner, score_a, score_b,
    win_points_a, win_points_b, active_a, active_b, rocks_a, rocks_b, turns, timeouts, invalid,
    started_at, finished_at, replay_key, image, game_version
) VALUES (
    :match_id, :batch_id, :seed, :strategy_a, :strategy_b, :status, :winner, :score_a, :score_b,
    :win_points_a, :win_points_b, :active_a, :active_b, :rocks_a, :rocks_b, :turns, :timeouts,
    :invalid, :started_at, :finished_at, :replay_key, :image, :game_version)
ON CONFLICT (match_id) DO NOTHING
"""


def log(message, **fields):
    print(json.dumps({"message": message, **fields}), flush=True)


def private_address():
    """This task's address in the VPC, from the metadata endpoint ECS gives every container."""
    with urllib.request.urlopen(os.environ["ECS_CONTAINER_METADATA_URI_V4"], timeout=10) as f:
        return json.load(f)["Networks"][0]["IPv4Addresses"][0]


def start_bot(ecs, slot, strategy, token, address):
    """Starts one bot in its own task. A bot that can't start voids the match (read_result)."""
    match_id, batch_id = os.environ["MATCH_ID"], os.environ["BATCH_ID"]
    # BOT_STRATEGY for bots with strategies, BOT_MODE for the sample bot (bots/python/bot.py).
    env = {
        "CONTROLLER_ADDR": f"{address}:{PORT}",
        "BOT_TOKEN": token,
        "BOT_STRATEGY": strategy,
        "BOT_MODE": strategy,
    }
    for attempt in range(6):
        # A new token for each attempt: ECS answers a repeated token with its first response, so
        # the same one would only repeat a failed placement (batch ec2-48).
        client_token = hashlib.sha256(f"{match_id}/{slot}/{attempt}".encode()).hexdigest()[:32]
        try:
            response = ecs.run_task(
                cluster=os.environ["MATCH_CLUSTER"],
                taskDefinition=os.environ["BOT_TASK_DEFINITION"],
                **bot_tasks.placement(
                    os.environ["BOT_SUBNETS"].split(","),
                    os.environ["BOT_SECURITY_GROUP"],
                    os.environ.get("BOT_CAPACITY_PROVIDER"),
                ),
                startedBy=batch_id,
                group=f"match:{match_id}",
                clientToken=client_token,
                overrides={
                    "containerOverrides": [
                        {
                            "name": "bot",
                            "environment": [{"name": k, "value": v} for k, v in env.items()],
                        }
                    ]
                },
            )
            if response["tasks"]:
                task = response["tasks"][0]["taskArn"]
                log("bot started", slot=slot, task=task)
                return task
            reason = response["failures"]
        except (BotoCoreError, ClientError) as e:
            reason = str(e)[:300]
        log("starting a bot failed", slot=slot, attempt=attempt, error=str(reason))
        time.sleep(random.uniform(1, 2**attempt))


def play_match(seed, strategies, out_dir):
    """Runs the controller here and both bots in their own tasks. Returns the controller's exit
    code and the bots' tasks. Bots end when the controller closes their connection, or at their
    image's deadline."""
    tokens = [secrets.token_hex(16) for _ in strategies]
    spec = {
        "match_id": os.environ["MATCH_ID"],
        "attempt": 1,
        "seed": seed,
        "players": [{"slot": slot, "token": token} for slot, token in enumerate(tokens)],
        "limits": {**LIMITS, "max_turns": int(os.environ["MAX_TURNS"])},
        "game": {},
    }
    controller = subprocess.Popen(
        [CONTROLLER],
        env={
            "MATCH_SPEC": json.dumps(spec),
            "PORT": PORT,
            "OUT_DIR": out_dir,
            "GAME_VERSION": os.environ.get("GAME_VERSION", "unknown"),
            # The mock controller's game (run_batch.py --game); other controllers ignore it.
            **({"MOCK_GAME": os.environ["MOCK_GAME"]} if "MOCK_GAME" in os.environ else {}),
        },
    )
    address = private_address()
    ecs = boto3.client("ecs", config=Config(retries={"max_attempts": 12, "mode": "adaptive"}))
    bots = [
        start_bot(ecs, slot, strategy, token, address)
        for slot, (strategy, token) in enumerate(zip(strategies, tokens, strict=True))
    ]
    if os.environ.get("BOT_CAPACITY_PROVIDER"):
        bot_tasks.drain_machines(
            ecs,
            os.environ["MATCH_CLUSTER"],
            [task for task in bots if task],
            LIMITS["connect_ms"] / 1000,
            log,
        )
    return controller.wait(), bots


def lost_machine(ecs, bots, out_dir):
    """Whether a bot that dropped out of the match lost its machine: AWS reclaimed it (Spot) or it
    failed. Such a match is voided, because the infrastructure must never decide a match. ECS
    stops a bot whose machine went away without an exit code, while a bot that crashes or quits
    has one. Waits up to two minutes for ECS to stop such a bot."""
    try:
        with open(f"{out_dir}/result.json") as f:
            outcomes = [p["outcome"] for p in json.load(f)["players"]]
    except FileNotFoundError:
        return False
    dropped = [
        task
        for task, outcome in zip(bots, outcomes, strict=False)
        if task and outcome in ("disconnected", "timeout")
    ]
    deadline = time.time() + 120
    while dropped and time.time() < deadline:
        try:
            described = ecs.describe_tasks(cluster=os.environ["MATCH_CLUSTER"], tasks=dropped)[
                "tasks"
            ]
        except (BotoCoreError, ClientError) as e:
            log("checking the bots failed", error=str(e)[:300])
            described = []
        for task in described:
            if task["lastStatus"] != "STOPPED":
                continue
            container = next((c for c in task.get("containers", []) if c["name"] == "bot"), {})
            if container.get("exitCode") is None:
                log(
                    "bot machine lost",
                    task=task["taskArn"],
                    reason=task.get("stoppedReason"),
                    stop_code=task.get("stopCode"),
                )
                return True
            dropped.remove(task["taskArn"])
        if dropped:
            time.sleep(10)
    return False


def read_result(out_dir):
    """The columns that come from result.json, empty if the controller wrote none."""
    try:
        with open(f"{out_dir}/result.json") as f:
            result = json.load(f)
    except FileNotFoundError:
        return {"status": "controller_error"}
    players = result["players"]
    scores = [p["score"] or 0 for p in players]
    # A bot that never connected is an infrastructure failure, so it must not count as a loss.
    if any(p["outcome"] == "never_connected" for p in players):
        return {"status": "void_bot_never_connected", "turns": result["turns"]}
    winner = None
    if result["status"] == "completed":
        winner = "draw" if scores[0] == scores[1] else "a" if scores[0] > scores[1] else "b"
    columns = {
        "status": result["status"],
        "winner": winner,
        "score_a": scores[0],
        "score_b": scores[1],
        "turns": result["turns"],
        "timeouts": sum(p["timeouts"] for p in players),
        "invalid": sum(p["invalid_actions"] for p in players),
    }
    if os.environ.get("SCORE_FORMAT") == "win-points":
        columns.update(win_point_columns(scores, result["turns"]))
    return columns


def win_point_columns(scores, turns):
    """The real game's ranking, out of its encoded scores. A game cut short by MAX_TURNS is ranked
    by win points only: the tie-breakers exist only at the end of a full game."""
    win_points = [score // WIN_POINT_WEIGHT for score in scores]
    rest = [score % WIN_POINT_WEIGHT for score in scores]
    full_game = turns >= FULL_GAME_TURNS
    return {
        "win_points_a": win_points[0],
        "win_points_b": win_points[1],
        "active_a": rest[0] // ACTIVE_ROVER_WEIGHT if full_game else None,
        "active_b": rest[1] // ACTIVE_ROVER_WEIGHT if full_game else None,
        "rocks_a": rest[0] % ACTIVE_ROVER_WEIGHT if full_game else None,
        "rocks_b": rest[1] % ACTIVE_ROVER_WEIGHT if full_game else None,
    }


def upload_replay(out_dir, batch_id, match_id):
    bucket = os.environ.get("REPLAY_BUCKET")
    path = f"{out_dir}/replay.json.gz"
    if not bucket or not os.path.exists(path):
        return None
    key = f"batches/{batch_id}/{match_id}/replay.json.gz"
    boto3.client("s3").upload_file(path, bucket, key)
    return key


def connect():
    import pg8000.native

    # Encrypted, but the server's certificate isn't verified: the database is only reachable
    # inside this VPC, and the image doesn't carry the RDS CA bundle.
    context = None
    if os.environ.get("DB_TLS", "on") == "on":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        ssl_context=context,
        timeout=30,
    )


def save_result(row):
    """Many tasks may finish at once and the database is small, so retry with jitter."""
    for attempt in range(1, 7):
        try:
            db = connect()
            try:
                # The lock makes concurrent CREATE TABLE and ALTER TABLE ... IF NOT EXISTS safe.
                db.run("BEGIN")
                db.run("SELECT pg_advisory_xact_lock(7001)")
                db.run(SCHEMA)
                for migration in MIGRATIONS:
                    db.run(migration)
                db.run("COMMIT")
                db.run(INSERT, **row)
            finally:
                db.close()
            return
        except Exception as e:
            log("saving the result failed", attempt=attempt, error=str(e)[:300])
            if attempt == 6:
                raise
            time.sleep(random.uniform(1, 2**attempt))


def play():
    match_id, batch_id = os.environ["MATCH_ID"], os.environ["BATCH_ID"]
    seed = int(os.environ["SEED"])
    strategies = [os.environ["STRATEGY_A"], os.environ["STRATEGY_B"]]
    started_at = datetime.now(UTC)
    log("match starting", match_id=match_id, batch_id=batch_id, seed=seed, strategies=strategies)
    with tempfile.TemporaryDirectory() as out_dir:
        code, bots = play_match(seed, strategies, out_dir)
        finished_at = datetime.now(UTC)
        seconds = (finished_at - started_at).total_seconds()
        log("match played", match_id=match_id, controller_exit=code, seconds=round(seconds, 1))
        columns = dict.fromkeys(
            [
                "winner",
                "score_a",
                "score_b",
                "win_points_a",
                "win_points_b",
                "active_a",
                "active_b",
                "rocks_a",
                "rocks_b",
                "turns",
                "timeouts",
                "invalid",
                "replay_key",
            ]
        )
        columns.update(read_result(out_dir))
        if (
            os.environ.get("BOT_CAPACITY_PROVIDER")
            and columns["status"] != "void_bot_never_connected"
        ):
            ecs = boto3.client(
                "ecs", config=Config(retries={"max_attempts": 12, "mode": "adaptive"})
            )
            if lost_machine(ecs, bots, out_dir):
                columns.update(status="void_bot_machine_lost", winner=None)
        columns["replay_key"] = upload_replay(out_dir, batch_id, match_id)
        log("replay uploaded", key=columns["replay_key"])
    row = {
        **columns,
        "match_id": match_id,
        "batch_id": batch_id,
        "seed": seed,
        "strategy_a": strategies[0],
        "strategy_b": strategies[1],
        "started_at": started_at,
        "finished_at": finished_at,
        "image": os.environ.get("IMAGE_REVISION", "unknown"),
        "game_version": os.environ.get("GAME_VERSION", "unknown"),
    }
    save_result(row)
    log("match saved", **{k: str(v) for k, v in row.items()}, controller_exit=code)
    return 0 if code == 0 else 1


def report(batch_id):
    db = connect()
    [[total, completed, turns, first_start, last_finish, avg_seconds, timeouts, invalid]] = db.run(
        """SELECT count(*), count(*) FILTER (WHERE status = 'completed'), coalesce(sum(turns), 0),
                  min(started_at), max(finished_at),
                  avg(extract(epoch FROM finished_at - started_at))::float,
                  coalesce(sum(timeouts), 0), coalesce(sum(invalid), 0)
           FROM match_results WHERE batch_id = :batch""",
        batch=batch_id,
    )
    # The most matches that were running at the same moment: +1 at each start, -1 at each finish.
    [[peak]] = db.run(
        """SELECT coalesce(max(running), 0) FROM (
             SELECT sum(change) OVER (ORDER BY at, change) AS running FROM (
               SELECT started_at AS at, 1 AS change FROM match_results WHERE batch_id = :batch
               UNION ALL
               SELECT finished_at, -1 FROM match_results WHERE batch_id = :batch) events) totals""",
        batch=batch_id,
    )
    statuses = db.run(
        "SELECT status, count(*) FROM match_results WHERE batch_id = :batch GROUP BY 1 ORDER BY 1",
        batch=batch_id,
    )
    matchups = db.run(
        """SELECT strategy_a, strategy_b, winner, count(*),
                  avg(coalesce(win_points_a, score_a))::float,
                  avg(coalesce(win_points_b, score_b))::float
           FROM match_results WHERE batch_id = :batch AND status = 'completed'
           GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
        batch=batch_id,
    )
    print(
        "REPORT "
        + json.dumps(
            {
                "batch": batch_id,
                "matches": total,
                "completed": completed,
                "statuses": {status: n for status, n in statuses},
                "peak_running_at_once": int(peak),
                "turns": int(turns),
                "first_start": str(first_start),
                "last_finish": str(last_finish),
                "wall_clock_seconds": (last_finish - first_start).total_seconds() if total else 0,
                "average_match_seconds": round(avg_seconds or 0, 1),
                "timeouts": int(timeouts),
                "invalid_actions": int(invalid),
                "matchups": [
                    {
                        "a": a,
                        "b": b,
                        "winner": w,
                        "matches": n,
                        "avg_points_a": pa,
                        "avg_points_b": pb,
                    }
                    for a, b, w, n, pa, pb in matchups
                ],
            }
        ),
        flush=True,
    )
    db.close()
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "play"
    sys.exit(play() if command == "play" else report(sys.argv[2]))
