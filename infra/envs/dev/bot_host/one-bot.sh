#!/bin/bash
# One task per bot machine (matches_bot_hosts.tf). Whoever starts a bot drains its machine as soon
# as ECS places the task (platform/prototype: match_task.py and run_batch.py), so ECS never places a
# second one there. This script ends the machine once that task has stopped: the launch template
# terminates it on shutdown. It reads only the ECS agent's local introspection API.
set -u
TASKS_URL=${TASKS_URL:-http://localhost:51678/v1/tasks}

# none: no task yet (or the agent isn't up); busy: a task hasn't stopped; stopped: all have.
state() {
  curl -fsS --max-time 5 "$TASKS_URL" 2>/dev/null | python3 -c '
import json, sys
tasks = json.load(sys.stdin)["Tasks"]
print("none" if not tasks else "stopped" if all(t["KnownStatus"] == "STOPPED" for t in tasks) else "busy")
' 2>/dev/null || echo none
}

until [ "$(state)" != none ]; do sleep 2; done
until [ "$(state)" = stopped ]; do sleep 1; done
# Time for the agent to report the stop. The machine is drained, so nothing new arrives meanwhile.
sleep 10
systemctl stop ecs
shutdown -h now
