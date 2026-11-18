#!/usr/bin/env bash
# Whether a saved plan may be applied without an approver ("auto_apply" in ci.json): every
# resource change only creates, updates, reads, or leaves a resource as it is. Anything that
# destroys, replaces or forgets (`removed` blocks) prints false, and the plan waits for an
# approver (docs/ci-cd.md). An unreadable plan prints false too.
#
#   plan-is-safe.sh PLAN_JSON    -> prints true or false
set -euo pipefail

if jq -e '[.resource_changes[]?.change.actions[] | select(IN("create", "update", "read", "no-op") | not)] | length == 0' \
  "${1:?}" >/dev/null 2>&1; then
  echo true
else
  echo false
fi
