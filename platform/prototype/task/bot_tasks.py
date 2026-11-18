"""Where a bot's task runs, and how its EC2 machine is kept to one bot.

Used by match_task.py, next to this file in the match image, and by run_batch.py for the probe.
"""

import time

from botocore.exceptions import BotoCoreError, ClientError


def placement(subnets, security_group, capacity_provider=None):
    """RunTask's arguments that place a bot: on Fargate, or on a machine of an EC2 capacity
    provider (Spot or on-demand). The task's network interface is in one of the bots' subnets,
    one per availability zone: on EC2, the one in its machine's zone."""
    network = {"subnets": list(subnets), "securityGroups": [security_group]}
    if capacity_provider:
        return {
            "capacityProviderStrategy": [{"capacityProvider": capacity_provider, "weight": 1}],
            "networkConfiguration": {"awsvpcConfiguration": network},
        }
    return {
        "launchType": "FARGATE",
        "networkConfiguration": {"awsvpcConfiguration": {**network, "assignPublicIp": "DISABLED"}},
    }


def drain_machines(ecs, cluster, tasks, timeout_s, log):
    """On EC2, a machine runs one bot in its life. Draining it as soon as its task is placed means
    ECS never places another task there, and the machine terminates itself after the task
    (infra/envs/dev/bot_host/one-bot.sh). Returns the tasks whose machines weren't drained."""
    waiting, deadline = set(tasks), time.time() + timeout_s
    while waiting and time.time() < deadline:
        try:
            for task in ecs.describe_tasks(cluster=cluster, tasks=sorted(waiting))["tasks"]:
                if machine := task.get("containerInstanceArn"):
                    ecs.update_container_instances_state(
                        cluster=cluster, containerInstances=[machine], status="DRAINING"
                    )
                    log("bot machine drained", task=task["taskArn"], machine=machine)
                    waiting.discard(task["taskArn"])
                elif task["lastStatus"] == "STOPPED":
                    waiting.discard(task["taskArn"])
        except (BotoCoreError, ClientError) as e:
            log("draining a bot machine failed", error=str(e)[:300])
        if waiting:
            time.sleep(2)
    for task in waiting:
        log("draining a bot machine failed", task=task, error="not placed in time")
    return waiting
