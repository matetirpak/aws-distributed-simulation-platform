#!/usr/bin/env python3
"""Starts a batch of match tasks, waits for them, and prints the batch's numbers.

  uv run --with boto3 prototype/run_batch.py start --games 100 --image-tag TAG --batch b100 \
      [--max-turns N] [--bots ec2] [--game coin|nim] [--bot-image IMAGE] [--strategies A:B,...]
  uv run --with boto3 prototype/run_batch.py report --batch b100 --image-tag TAG
  uv run --with boto3 prototype/run_batch.py probe --batch p1 --image-tag TAG [--bots ec2]

Each match is one RunTask call here, and that task starts its two bots in tasks of their own.

  --image-tag   the match image, from push-image.sh: this repository's mock controller playing the
                toy games, or any game's controller image
  --game        the mock controller's game
  --bot-image   the bots' image: a tag in the bot repository, or a full image reference. Defaults
                to the sample bot pushed with the match image
  --strategies  the pairs of strategies the matches cycle through, passed to the bots as
                BOT_STRATEGY and BOT_MODE (default ok:ok, for the sample bot)
  --max-turns   default 10,000; a game cut short is ranked by its scores at that turn
  --bots        Fargate, or ec2 for each bot on an EC2 machine of its own: Spot first, and
                on-demand for what Spot can't supply

With --bots ec2 a match starts whenever two free machines are there, so a batch larger than the
quotas allow at once, or than Spot can supply, still completes, and a match the infrastructure
voided is played again with the same seed, up to three times in all. `probe` checks from inside a
bot task what a bot can reach. Starting a batch again with the same name starts only the matches it
hasn't started: the seeds are derived from the batch name.
"""

import argparse
import contextlib
import hashlib
import json
import math
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parent / "task"))
import bot_tasks  # shared with the match task: task/bot_tasks.py

PREFIX = "dsim-dev-matches"
BOTS = f"{PREFIX}-bot"
# The EC2 capacity providers, each named like its Auto Scaling group. A bot machine has 1 vCPU.
BOT_HOSTS = f"{PREFIX}-bot-hosts"  # on-demand
BOT_HOSTS_SPOT = f"{PREFIX}-bot-hosts-spot"
EC2_VCPU_QUOTAS = {
    BOT_HOSTS: "L-1216C47A",  # Running On-Demand Standard instances
    BOT_HOSTS_SPOT: "L-34B43A08",  # All Standard Spot Instance Requests
}
FARGATE_VCPU = 0.25  # each Fargate task: the controller, and the bots unless they run on EC2
FARGATE_VCPU_QUOTA = "L-3032A538"
SPOT_GRACE = 120  # seconds before on-demand machines make up for Spot machines that don't come
SPARE = 0.01  # extra machines, at least 4, so a bot whose machine AWS reclaims starts at once
POLL = 15
LABELS = {BOT_HOSTS_SPOT: "Spot", BOT_HOSTS: "on-demand"}
ATTEMPTS = 3  # a match the infrastructure voided is played again, up to this many times in all
MATCHUPS = [("greedy", "random"), ("random", "greedy"), ("greedy", "greedy")]

aws = Config(retries={"max_attempts": 12, "mode": "adaptive"}, max_pool_connections=16)


def network(ec2):
    """Where match tasks run: the match subnets, one per zone, with a public address."""
    names = [f"{PREFIX}-tasks", f"{PREFIX}-tasks-*"]
    subnet = ec2.describe_subnets(Filters=[{"Name": "tag:Name", "Values": names}])["Subnets"]
    group = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-tasks"]}]
    )
    return {
        "awsvpcConfiguration": {
            "subnets": [s["SubnetId"] for s in subnet],
            "securityGroups": [g["GroupId"] for g in group["SecurityGroups"]],
            "assignPublicIp": "ENABLED",
        }
    }


def task_definition(ecs, family, image):
    """A revision of Terraform's task definition that runs `image`: a tag in the family's own
    repository, or a full image reference."""
    current = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
    container = current["containerDefinitions"][0]
    repository = container["image"].rsplit(":", 1)[0]
    target = image if "/" in image else f"{repository}:{image}"
    if container["image"] == target:
        return current["taskDefinitionArn"]
    container["image"] = target
    keys = (
        "family",
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "containerDefinitions",
        "requiresCompatibilities",
        "cpu",
        "memory",
        "runtimePlatform",
    )
    return ecs.register_task_definition(**{k: current[k] for k in keys if k in current})[
        "taskDefinition"
    ]["taskDefinitionArn"]


def bot_network(ec2):
    """The bots' subnets, one per availability zone, and their security group: no internet
    route, and only the controller to talk to. ECS puts a bot on EC2 in the subnet of its
    machine's zone."""
    names = [f"{PREFIX}-bots", f"{PREFIX}-bots-*"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "tag:Name", "Values": names}])["Subnets"]
    group = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-bots"]}]
    )
    return sorted(s["SubnetId"] for s in subnets), group["SecurityGroups"][0]["GroupId"]


def quota(service, code):
    return boto3.client("service-quotas").get_service_quota(ServiceCode=service, QuotaCode=code)[
        "Quota"
    ]["Value"]


def groups(autoscaling):
    """The two bot machine groups by name: their maximum, desired and live machines."""
    found = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=list(EC2_VCPU_QUOTAS))
    return {g["AutoScalingGroupName"]: g for g in found["AutoScalingGroups"]}


def plan_capacity(games, bots):
    """How many matches can run at once. Bots on Fargate: refuses a batch that doesn't fit at
    once. Bots on EC2: the rest of a larger batch starts as earlier matches end. Returns the
    matches at once and, for EC2, the machines each group may have (its maximum and quota)."""
    fargate = quota("fargate", FARGATE_VCPU_QUOTA)
    if bots != "ec2":
        if games * 3 * FARGATE_VCPU > fargate:
            sys.exit(
                f"{games} matches need {games * 3 * FARGATE_VCPU:g} Fargate vCPU; "
                f"the quota is {fargate:g}"
            )
        return games, {}
    found = groups(boto3.client("autoscaling", config=aws))
    caps = {
        name: int(min(found[name]["MaxSize"], quota("ec2", code)))
        for name, code in EC2_VCPU_QUOTAS.items()
    }
    at_once = min(games, int(fargate / FARGATE_VCPU), sum(caps.values()) // 2)
    print(
        f"{games} matches, at most {at_once} at once: Fargate {fargate:g} vCPU, "
        f"bot machines {caps[BOT_HOSTS_SPOT]} Spot and {caps[BOT_HOSTS]} on-demand",
        flush=True,
    )
    return at_once, caps


def bot_placement(ec2, bots, provider=None):
    """The parts of RunTask that say where bots run, and the environment that tells match tasks.
    On EC2, run_batch adds each match's capacity provider to that environment."""
    subnets, group = bot_network(ec2)
    placement = bot_tasks.placement(
        subnets, group, (provider or BOT_HOSTS) if bots == "ec2" else None
    )
    return placement, {"BOT_SUBNETS": ",".join(subnets), "BOT_SECURITY_GROUP": group}


def machines(ecs):
    """The registered bot machines of each group: idle, which can take a bot; busy, with a bot;
    and ending, drained after their one bot and about to terminate."""
    arns = [
        a
        for page in ecs.get_paginator("list_container_instances").paginate(cluster=PREFIX)
        for a in page["containerInstanceArns"]
    ]
    found = {name: {"idle": [], "busy": [], "ending": []} for name in EC2_VCPU_QUOTAS}
    for i in range(0, len(arns), 100):
        for machine in ecs.describe_container_instances(
            cluster=PREFIX, containerInstances=arns[i : i + 100]
        )["containerInstances"]:
            group = found.get(machine.get("capacityProviderName"))
            if group is None or machine["status"] not in ("ACTIVE", "DRAINING"):
                continue
            busy = machine["runningTasksCount"] + machine["pendingTasksCount"]
            state = "busy" if busy else "idle" if machine["status"] == "ACTIVE" else "ending"
            group[state].append(machine["ec2InstanceId"])
    return found


def start_bot_machines(ecs, count, group=BOT_HOSTS):
    """Starts `count` machines in one group and waits for them, for the probe. A machine runs one
    bot in its life, so machines still busy or drained from an earlier batch don't count. The
    capacity provider removes idle machines after about 15 minutes without a bot."""
    autoscaling = boto3.client("autoscaling", config=aws)
    current = groups(autoscaling)[group]
    found = machines(ecs)[group]
    unusable = len(found["busy"]) + len(found["ending"])
    desired = min(current["MaxSize"], max(current["DesiredCapacity"], unusable + count))
    autoscaling.set_desired_capacity(AutoScalingGroupName=group, DesiredCapacity=desired)
    begin = time.time()
    while (ready := len(machines(ecs)[group]["idle"])) < count:
        if time.time() - begin > 600:
            sys.exit(f"only {ready} of {count} bot machines were ready after 10 minutes")
        print(f"{time.time() - begin:6.0f} s: {ready} of {count} bot machines ready", flush=True)
        time.sleep(POLL)
    print(f"{count} bot machines ready after {time.time() - begin:.0f} s")


def stop_idle_machines(ecs):
    """Terminates the bot machines without a task, and sets each group to its busy ones, instead of
    leaving them to the capacity provider's scale-in about 15 minutes later. Machines with a task
    are left alone; run one EC2 batch at a time, or another batch may lose machines it started."""
    autoscaling = boto3.client("autoscaling", config=aws)
    stopped = []
    for group, found in machines(ecs).items():
        for instance in found["idle"]:
            try:
                autoscaling.terminate_instance_in_auto_scaling_group(
                    InstanceId=instance, ShouldDecrementDesiredCapacity=True
                )
                stopped.append(instance)
            except autoscaling.exceptions.ClientError as e:  # already terminating, for example
                print(f"{instance}: {e}", file=sys.stderr)
        # Machines that end themselves after their bot still count in the group's desired capacity
        # until it notices, and it would replace them. Only the busy machines are wanted.
        autoscaling.set_desired_capacity(
            AutoScalingGroupName=group, DesiredCapacity=len(found["busy"])
        )
    return len(stopped)


def print_event(message, **fields):
    print(message, json.dumps(fields), flush=True)


def bot_definition(ecs, args):
    """The bots' task definition, for Fargate or EC2, running --bot-image: by default the sample
    bot that push-image.sh pushed with the match image (its tag is the commit part)."""
    image = args.bot_image or args.image_tag.split("-")[0]
    return task_definition(ecs, f"{BOTS}-ec2" if args.bots == "ec2" else BOTS, image)


def matchups(args):
    pairs = [tuple(pair.split(":", 1)) for pair in args.strategies.split(",")]
    if any(len(pair) != 2 or not all(pair) for pair in pairs):
        sys.exit("--strategies takes pairs like greedy:random,random:greedy")
    return pairs


def active_tasks(ecs, batch):
    paginator = ecs.get_paginator("list_tasks")
    return [
        a
        for page in paginator.paginate(cluster=PREFIX, startedBy=batch, desiredStatus="RUNNING")
        for a in page["taskArns"]
    ]


def match_id(batch, index, attempt):
    """A match's ID: the batch and its number, plus the attempt from the second one on."""
    return f"{batch}-{index:05d}" if attempt == 1 else f"{batch}-{index:05d}-{attempt}"


def run_task(ecs, definition, net, bots, pairs, batch, index, attempt=1):
    # The same seed for every attempt: a voided match is the same game played again.
    digest = hashlib.sha256(f"{batch}/{index}".encode()).hexdigest()
    a, b = pairs[index % len(pairs)]
    env = {
        "BATCH_ID": batch,
        "MATCH_ID": match_id(batch, index, attempt),
        "SEED": str(int(digest[:8], 16)),
        "STRATEGY_A": a,
        "STRATEGY_B": b,
        **bots,
    }
    for tries in range(8):
        response = ecs.run_task(
            cluster=PREFIX,
            taskDefinition=definition,
            launchType="FARGATE",
            networkConfiguration=net,
            startedBy=batch,
            # A new token for each attempt: ECS answers a repeated token with its first response,
            # so the same one would only repeat the failure.
            clientToken=hashlib.sha256(
                (
                    f"{batch}/{index}/{tries}"
                    if attempt == 1
                    else f"{batch}/{index}/a{attempt}/{tries}"
                ).encode()
            ).hexdigest()[:32],
            overrides={
                "containerOverrides": [
                    {
                        "name": "match",
                        "environment": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ]
            },
        )
        if response["tasks"]:
            return True
        print(f"match {index}: {response['failures']}; retrying", file=sys.stderr)
        time.sleep(2**tries)
    return False


def batch_tasks(ecs, batch):
    """The batch's tasks that haven't stopped, described: match tasks and bot tasks."""
    arns = active_tasks(ecs, batch)
    return [
        t
        for i in range(0, len(arns), 100)
        for t in ecs.describe_tasks(cluster=PREFIX, tasks=arns[i : i + 100])["tasks"]
    ]


def environment(task):
    return {
        e["name"]: e["value"]
        for o in task.get("overrides", {}).get("containerOverrides", [])
        for e in o.get("environment", [])
    }


def unplaced_bots(tasks, placed):
    """For each group, the bots of running matches that have no machine yet: not started by their
    match task so far, or waiting to be placed. Idle machines minus these are free. `placed` keeps
    every bot seen on a machine, per match: a bot that has stopped while its match task still
    writes the result is no longer listed, but it doesn't need a machine."""
    for task in tasks:
        if task.get("group", "").startswith("match:") and task.get("containerInstanceArn"):
            placed.setdefault(task["group"], set()).add(task["taskArn"])
    waiting = dict.fromkeys(EC2_VCPU_QUOTAS, 0)
    for task in tasks:
        env = environment(task)
        if task.get("launchType") == "FARGATE" and env.get("BOT_CAPACITY_PROVIDER") in waiting:
            seen = placed.get(f"match:{env['MATCH_ID']}", ())
            waiting[env["BOT_CAPACITY_PROVIDER"]] += max(0, 2 - len(seen))
    return waiting


class Machines:
    """The batch's bot machines: Spot first, and on-demand for what Spot can't supply."""

    def __init__(self, ecs, at_once, caps):
        self.ecs, self.caps, self.begin = ecs, caps, time.time()
        self.autoscaling = boto3.client("autoscaling", config=aws)
        self.want = 2 * at_once
        # Spare machines: when AWS reclaims a Spot machine, ECS drains it, and a bot meant for it
        # would wait minutes for a replacement (batch spot-200). A spare one takes it at once.
        self.spare = max(4, math.ceil(SPARE * self.want))
        spot = min(self.want + self.spare, caps[BOT_HOSTS_SPOT])
        self.base = min(
            self.want + self.spare - spot, caps[BOT_HOSTS]
        )  # on-demand beyond the Spot quota
        self.targets = {BOT_HOSTS_SPOT: spot, BOT_HOSTS: self.base}
        # Machines still busy or drained from an earlier batch can't take a bot, but they count.
        self.held = {
            g: len(found["busy"]) + len(found["ending"]) for g, found in machines(ecs).items()
        }
        self.shortfalls, self.asked, self.peak, self.used = [], {}, {}, set()
        self.ending, self.handed = set(), set()
        for group in self.targets:
            self.ask(group)

    def ask(self, group, desired=None):
        if desired is None:
            desired = min(self.caps[group], self.held[group] + self.targets[group])
        self.autoscaling.set_desired_capacity(AutoScalingGroupName=group, DesiredCapacity=desired)
        self.asked[group] = desired

    def hand_over(self, found):
        """Tells each group about machines that ended, so it doesn't count them as live. A machine
        shuts itself down after its bot, and its group notices only minutes later: in the batch
        spot-1000 it still listed over 1,000 machines that were gone, and launched nothing for the
        last matches. So machines that ended (drained with no task, seen in two polls) and machines
        the group lists but ECS no longer knows and EC2 reports gone are terminated through the
        group, without lowering it; balance() decides whether they're replaced."""
        for group in found.values():  # every poll, so balance() knows which machines ran a bot
            self.used.update(group["busy"])
        ending = {i for f in found.values() for i in f["ending"]}
        done = (ending & self.ending) - self.handed
        self.ending = ending
        known = {i for f in found.values() for state in f.values() for i in state}
        listed = [
            i["InstanceId"]
            for g in groups(self.autoscaling).values()
            for i in g["Instances"]
            if i["LifecycleState"] == "InService" and i["InstanceId"] not in known
        ]
        ec2 = boto3.client("ec2", config=aws)
        for k in range(0, len(listed), 500):
            for r in ec2.describe_instances(InstanceIds=listed[k : k + 500])["Reservations"]:
                done.update(
                    i["InstanceId"] for i in r["Instances"] if i["State"]["Name"] == "terminated"
                )
        for instance in done - self.handed:
            # A machine the group is already terminating is fine as it is.
            with contextlib.suppress(self.autoscaling.exceptions.ClientError):
                self.autoscaling.terminate_instance_in_auto_scaling_group(
                    InstanceId=instance, ShouldDecrementDesiredCapacity=False
                )
            self.handed.add(instance)

    def balance(self, found, needed):
        """Sets each group to its busy machines, plus machines AWS is reclaiming (drained before
        they ran a bot), plus what the batch still needs: `needed` bots to place and the spares,
        Spot first. On-demand covers what Spot has shown it can't supply: the most Spot machines
        live at once, minus the busy ones. Never more than first asked, plus the reclaimed ones.
        So a reclaimed machine is replaced at once, and machines that end after their match aren't
        replaced in vain."""
        live = {
            g: len(
                [i for i in group["Instances"] if i["LifecycleState"] in ("Pending", "InService")]
            )
            for g, group in groups(self.autoscaling).items()
        }
        self.peak = {g: max(self.peak.get(g, 0), live[g]) for g in live}
        for group in found.values():
            self.used.update(group["busy"])
        spot_can = max(0, self.peak[BOT_HOSTS_SPOT] - len(found[BOT_HOSTS_SPOT]["busy"]))
        shares = {
            BOT_HOSTS_SPOT: min(needed, self.targets[BOT_HOSTS_SPOT]),
            BOT_HOSTS: min(self.targets[BOT_HOSTS], max(0, needed - spot_can)),
        }
        for group, share in shares.items():
            reclaimed = len([i for i in found[group]["ending"] if i not in self.used])
            ceiling = self.held[group] + self.targets[group] + reclaimed
            desired = min(self.caps[group], ceiling, len(found[group]["busy"]) + reclaimed + share)
            if desired != self.asked[group]:
                self.ask(group, desired)

    def top_up(self):
        """After a grace period, on-demand machines make up for Spot machines that don't come, as
        far as the on-demand quota goes. The smallest shortfall of the last minute, so that machines
        ending after their match and their replacements don't count."""
        if time.time() - self.begin < SPOT_GRACE:
            return
        spot = groups(self.autoscaling)[BOT_HOSTS_SPOT]
        live = [i for i in spot["Instances"] if i["LifecycleState"] in ("Pending", "InService")]
        self.shortfalls = [*self.shortfalls, max(0, spot["DesiredCapacity"] - len(live))][-4:]
        target = min(self.caps[BOT_HOSTS], self.base + min(self.shortfalls))
        if target > self.targets[BOT_HOSTS]:
            print(
                f"Spot is {min(self.shortfalls)} machines short; asking on-demand for {target}",
                flush=True,
            )
            self.targets[BOT_HOSTS] = target
            self.ask(BOT_HOSTS)

    def warm_up(self):
        """Waits until the first wave's machines are ready, or no new one came for 3 minutes, or 15
        minutes passed. So as many matches as possible start together."""
        best, since = 0, time.time()
        while (ready := sum(len(f["idle"]) for f in machines(self.ecs).values())) < self.want:
            if ready > best:
                best, since = ready, time.time()
            elif time.time() - since > 180 or time.time() - self.begin > 900:
                print(
                    f"starting with {ready} of {self.want} bot machines; "
                    "the rest start as machines come"
                )
                return
            print(
                f"{time.time() - self.begin:6.0f} s: {ready} of {self.want} bot machines ready",
                flush=True,
            )
            self.top_up()
            time.sleep(POLL)
        print(f"{ready} bot machines ready after {time.time() - self.begin:.0f} s")


def saved_matches(logs, batch, since, status):
    """The batch's matches that their match tasks saved with `status` (a prefix with *), from the
    match logs: match ID to status."""
    pattern = f'{{ $.message = "match saved" && $.batch_id = "{batch}" && $.status = "{status}" }}'
    found = {}
    paginator = logs.get_paginator("filter_log_events")
    for page in paginator.paginate(
        logGroupName=f"/ecs/{PREFIX}",
        logStreamNamePrefix="match/",
        startTime=since,
        filterPattern=pattern,
    ):
        for event in page["events"]:
            line = json.loads(event["message"])
            found[line["match_id"]] = line["status"]
    return found


def start_waves(args, ecs, definition, net, env, pairs, at_once, caps):
    """Bots on EC2: starts a match whenever two free machines of a group are there, Spot first,
    with at most `at_once` matches running. A match the infrastructure voided (a bot that never
    connected, or lost its machine) is played again, up to ATTEMPTS times in all. Returns when all
    of the batch's tasks have stopped."""
    fleet = Machines(ecs, at_once, caps)
    fleet.warm_up()
    logs = boto3.client("logs", config=aws)
    begin, started, peak, replays = time.time(), 0, 0, 0
    since = int(begin * 1000) - 600_000
    queue = deque((index, 1) for index in range(args.games))
    attempt_of = {match_id(args.batch, i, 1): (i, 1) for i in range(args.games)}
    seen_voids, placed, progress, last_look = set(), {}, begin, False
    while True:
        tasks = batch_tasks(ecs, args.batch)
        running = sum(1 for t in tasks if t.get("launchType") == "FARGATE")
        peak = max(peak, running)
        if tasks:
            progress, last_look = time.time(), False
        # Voided matches go back in the queue, with the next attempt.
        for mid, status in saved_matches(logs, args.batch, since, "void*").items():
            if mid in seen_voids or mid not in attempt_of:
                continue
            seen_voids.add(mid)
            index, attempt = attempt_of[mid]
            if attempt < ATTEMPTS:
                queue.append((index, attempt + 1))
                attempt_of[match_id(args.batch, index, attempt + 1)] = (index, attempt + 1)
                replays += 1
                print(f"{mid}: {status}; playing it again", flush=True)
            else:
                print(f"{mid}: {status} on its last attempt", flush=True)
        if not queue and not tasks:
            if last_look:
                break
            last_look = True  # a void is logged as its task ends: look once more
            time.sleep(POLL)
            continue
        if not tasks and time.time() - progress > 900:
            print(f"no bot machines for 15 minutes; stopping with {len(queue)} matches not started")
            break
        found, waiting = machines(ecs), unplaced_bots(tasks, placed)
        if queue:
            room, jobs = at_once - running, []
            for group in (BOT_HOSTS_SPOT, BOT_HOSTS):
                for _ in range(max(0, len(found[group]["idle"]) - waiting[group]) // 2):
                    if room <= 0 or not queue:
                        break
                    jobs.append((*queue.popleft(), group))
                    room -= 1
            with ThreadPoolExecutor(8) as pool:
                started += sum(
                    pool.map(
                        lambda job: run_task(
                            ecs,
                            definition,
                            net,
                            {**env, "BOT_CAPACITY_PROVIDER": job[2]},
                            pairs,
                            args.batch,
                            job[0],
                            job[1],
                        ),
                        jobs,
                    )
                )
            fleet.top_up()
            fleet.balance(
                found, 2 * len(queue) + sum(waiting.values()) + 2 * len(jobs) + fleet.spare
            )
        elif any(waiting.values()):
            # Every match has started, but some bots still wait, for a reclaimed machine say.
            fleet.balance(found, sum(waiting.values()) + fleet.spare)
        else:
            # Every match has its machines, so idle ones are no longer needed, and neither are
            # replacements for machines that ended after their match.
            stop_idle_machines(ecs)
        # After the groups are set, so a machine handed over isn't replaced before they are lowered.
        fleet.hand_over(found)
        print(
            f"{time.time() - begin:6.0f} s: {started} started ({replays} replays), "
            f"{running} running "
            f"(peak {peak}); "
            + "; ".join(
                f"{LABELS[g]} {len(f['idle'])} idle, {len(f['busy'])} busy"
                for g, f in found.items()
            ),
            flush=True,
        )
        time.sleep(POLL)
    completed = {
        attempt_of[mid][0]
        for mid in saved_matches(logs, args.batch, since, "completed")
        if mid in attempt_of
    }
    print(
        f"all stopped after {time.time() - begin:.0f} s; "
        f"{len(completed)} of {args.games} matches completed, "
        f"{replays} played again after a void; at most {peak} running at once"
    )
    print(f"Stopped {stop_idle_machines(ecs)} idle bot machines.")


def start(args):
    at_once, caps = plan_capacity(args.games, args.bots)
    pairs = matchups(args)
    ecs, ec2 = boto3.client("ecs", config=aws), boto3.client("ec2", config=aws)
    definition, net = task_definition(ecs, PREFIX, args.image_tag), network(ec2)
    _, placement = bot_placement(ec2, args.bots)
    bots = {
        "MAX_TURNS": str(args.max_turns),
        "MOCK_GAME": args.game,
        "MATCH_CLUSTER": PREFIX,
        "BOT_TASK_DEFINITION": bot_definition(ecs, args),
        **placement,
    }
    if args.bots == "ec2":
        start_waves(args, ecs, definition, net, bots, pairs, at_once, caps)
        report(args)
        return
    begin = time.time()
    with ThreadPoolExecutor(8) as pool:
        started = list(
            pool.map(
                lambda i: run_task(ecs, definition, net, bots, pairs, args.batch, i),
                range(args.games),
            )
        )
    print(f"started {sum(started)}/{args.games} in {time.time() - begin:.0f} s with {definition}")
    peak = 0
    while running := len(active_tasks(ecs, args.batch)):
        peak = max(peak, running)
        print(f"{time.time() - begin:6.0f} s: {running} tasks active (peak {peak})", flush=True)
        time.sleep(30)
    print(f"all stopped after {time.time() - begin:.0f} s")
    report(args)


def report(args):
    ecs, ec2, logs = (boto3.client(c, config=aws) for c in ("ecs", "ec2", "logs"))
    definition = task_definition(ecs, PREFIX, args.image_tag)
    task = ecs.run_task(
        cluster=PREFIX,
        taskDefinition=definition,
        launchType="FARGATE",
        networkConfiguration=network(ec2),
        startedBy=f"report-{args.batch}"[:36],
        overrides={"containerOverrides": [{"name": "match", "command": ["report", args.batch]}]},
    )["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=PREFIX, tasks=[task], WaiterConfig={"MaxAttempts": 40}
    )
    stream = f"match/match/{task.rsplit('/', 1)[1]}"
    events = logs.get_log_events(logGroupName=f"/ecs/{PREFIX}", logStreamName=stream)["events"]
    for event in events:
        if event["message"].startswith("REPORT "):
            print(json.dumps(json.loads(event["message"][7:]), indent=2))
            return
    sys.exit("no report in the task's log:\n" + "\n".join(e["message"] for e in events))


def probe(args):
    """Two bot tasks: the first only listens, the second tries to reach it and everything else."""
    ecs, ec2, logs = (boto3.client(c, config=aws) for c in ("ecs", "ec2", "logs"))
    rds = boto3.client("rds", config=aws)
    db_host = rds.describe_db_instances(DBInstanceIdentifier=PREFIX)["DBInstances"][0]["Endpoint"][
        "Address"
    ]
    definition = bot_definition(ecs, args)
    placement, _ = bot_placement(ec2, args.bots)
    if args.bots == "ec2":
        start_bot_machines(ecs, 2)

    def run(env):
        return ecs.run_task(
            cluster=PREFIX,
            taskDefinition=definition,
            **placement,
            startedBy=args.batch,
            overrides={
                "containerOverrides": [
                    {
                        "name": "bot",
                        "command": ["python3", "/app/isolation_probe.py"],
                        "environment": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ]
            },
        )["tasks"][0]["taskArn"]

    listener = run({"PROBE_LISTEN_SECONDS": "300"})
    if args.bots == "ec2":
        bot_tasks.drain_machines(ecs, PREFIX, [listener], 600, print_event)
    ecs.get_waiter("tasks_running").wait(cluster=PREFIX, tasks=[listener])
    details = ecs.describe_tasks(cluster=PREFIX, tasks=[listener])["tasks"][0]["attachments"][0][
        "details"
    ]
    peer = next(d["value"] for d in details if d["name"] == "privateIPv4Address")
    prober = run({"PROBE_PEER": peer, "PROBE_DB_HOST": db_host})
    if args.bots == "ec2":
        bot_tasks.drain_machines(ecs, PREFIX, [prober], 600, print_event)
    ecs.get_waiter("tasks_stopped").wait(
        cluster=PREFIX, tasks=[prober], WaiterConfig={"MaxAttempts": 40}
    )
    stream = f"bot/bot/{prober.rsplit('/', 1)[1]}"
    for event in logs.get_log_events(logGroupName=f"/ecs/{PREFIX}", logStreamName=stream)["events"]:
        print(event["message"])
    ecs.stop_task(cluster=PREFIX, task=listener, reason="probe finished")
    if args.bots == "ec2":
        ecs.get_waiter("tasks_stopped").wait(cluster=PREFIX, tasks=[listener, prober])
        print(f"Stopped {stop_idle_machines(ecs)} idle bot machines.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["start", "report", "probe"])
    parser.add_argument("--batch", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=10_000)
    parser.add_argument("--bots", choices=["fargate", "ec2"], default="fargate")
    parser.add_argument("--game", choices=["coin", "nim"], default="coin")
    parser.add_argument("--bot-image")
    parser.add_argument("--strategies", default="ok:ok")
    args = parser.parse_args()
    {"start": start, "report": report, "probe": probe}[args.command](args)


if __name__ == "__main__":
    main()
