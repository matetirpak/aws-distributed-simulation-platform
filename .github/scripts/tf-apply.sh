#!/usr/bin/env bash
# Apply exactly a saved plan. Shared by apply.yml (an approver applies) and the auto-apply
# job in _terraform-env.yml (applied after a merge).
#
#   TF_ENV=<env> PLAN_DIR=<dir with tfplan> TF_STATE_BUCKET=<bucket> tf-apply.sh
#
# TF_STATE_BUCKET comes from read-ci-config.sh. The saved plan already holds the root's inputs.
#
# Writes the output to $RUNNER_TEMP/apply.log.
set -euo pipefail

: "${TF_ENV:?}" "${PLAN_DIR:?}"
[[ "$TF_ENV" =~ ^[a-z0-9-]+$ ]] || { echo "::error::invalid environment name" >&2; exit 2; }

root="$(git rev-parse --show-toplevel)/infra/envs/$TF_ENV"
log="${RUNNER_TEMP:-/tmp}/apply.log"

terraform -chdir="$root" init -input=false -lockfile=readonly -no-color -backend-config="bucket=${TF_STATE_BUCKET:?}"
rc=0
terraform -chdir="$root" apply -input=false -no-color "$PLAN_DIR/tfplan" 2>&1 | tee "$log" || rc=$?
if ((rc != 0)); then
  if grep -q "Saved plan is stale" "$log"; then
    echo "::error::The saved plan is stale: the state changed after it was made. Re-run deploy (gh workflow run deploy.yml --ref main) and approve the new plan."
  else
    echo "::error::terraform apply failed. Fix forward with a new PR (docs/ci-cd.md)."
  fi
  exit "$rc"
fi
