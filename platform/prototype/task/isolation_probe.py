"""Runs inside a bot task instead of the bot, and reports what a bot can reach as one JSON line.

Started by `run_batch.py probe`. It shows what the task's real security group, subnet and role
allow, which reading the Terraform can't. The expected result is in
docs/architecture.md.

  PROBE_LISTEN_SECONDS  only listen on port 9000, so another probe has something to reach
  PROBE_PEER            the listening probe's private address
  PROBE_DB_HOST         the results database
"""

import json
import os
import re
import socket
import time

BOT_PORT = 9000


def connect(host, port):
    """`blocked` is a timeout, which is what a security group or a missing route looks like."""
    try:
        with socket.create_connection((host, port), timeout=5):
            return "connected"
    except TimeoutError:
        return "blocked"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return f"error: {e}"


def resolve(name):
    try:
        return socket.gethostbyname(name)
    except OSError as e:
        return f"error: {e}"


def can_write(path):
    try:
        with open(path, "w") as f:
            f.write("x")
        os.remove(path)
        return True
    except OSError:
        return False


def effective_capabilities():
    with open("/proc/self/status") as f:
        return re.search(r"CapEff:\s+(\w+)", f.read()).group(1)


def other_processes():
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                found.append(f.read().replace(b"\0", b" ").decode().strip())
        except OSError:
            pass
    return found


def main():
    listen_seconds = int(os.environ.get("PROBE_LISTEN_SECONDS", "0"))
    if listen_seconds:
        with socket.create_server(("", BOT_PORT)):
            time.sleep(listen_seconds)
        return
    region = os.environ.get("AWS_REGION", "eu-central-1")
    print(
        json.dumps(
            {
                "message": "isolation probe",
                "reach": {
                    "other_bot_9000": connect(os.environ["PROBE_PEER"], BOT_PORT),
                    "database_5432": connect(os.environ["PROBE_DB_HOST"], 5432),
                    "internet_ip_443": connect("1.1.1.1", 443),
                    "internet_name_443": connect("example.com", 443),
                    "ecs_api_443": connect(f"ecs.{region}.amazonaws.com", 443),
                    "instance_metadata_80": connect("169.254.169.254", 80),
                    "ecr_endpoint_443": connect(f"api.ecr.{region}.amazonaws.com", 443),
                },
                "public_name_resolves_to": resolve("example.com"),
                "secrets_in_environment": sorted(
                    name
                    for name in os.environ
                    if re.search("PASSWORD|SECRET|TOKEN|AWS_CONTAINER_CREDENTIALS", name)
                ),
                "other_processes": other_processes(),
                "can_write_root_filesystem": can_write("/app/probe-write-test"),
                "can_write_tmp": can_write("/tmp/probe-write-test"),
                "effective_capabilities": effective_capabilities(),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
