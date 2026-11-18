#!/usr/bin/env bash
# Plan one environment and render the result. Shared by ci.yml, _terraform-env.yml
# and drift.yml so every plan is produced the same way.
#
# Inputs (environment variables):
#   TF_ENV      environment name; the root is infra/envs/$TF_ENV
#   OUT_DIR     where to write tfplan, plan.txt, plan.json, plan.log, comment.md, summary.md
#   COMMIT_SHA, RUN_URL, EXTRA_MD   passed through to render-plan.sh
#   ASSUME_ROLE_OUTCOME   outcome of the credentials step (that step uses
#               continue-on-error, so a failure still produces a ❌ comment)
#   PLAN_ROLE   role ARN, for the error message
#   PLAN_MODE   "normal" (default) or "refresh-only" (drift detection: only
#               changes made outside Terraform, not pending unapplied merges)
#   TF_STATE_BUCKET, TF_CI_TFVARS   from read-ci-config.sh: the state bucket, and the root's
#               account-specific inputs, written to a git-ignored ci.auto.tfvars.json
#
# Always exits 0 so the caller can publish the rendered result first. The plan's
# exit code is written to $OUT_DIR/exitcode and to the step outputs
# (exitcode, has_changes, auto_safe). Callers must fail the job when exitcode is not 0 or 2.
# auto_safe is true only for a plan with changes that destroys, replaces and forgets
# nothing (plan-is-safe.sh); such a plan may be applied after a merge without an approver.
#
# plan.json contains sensitive values in plain text: never upload or print it.
set -euo pipefail

: "${TF_ENV:?}" "${OUT_DIR:?}"
[[ "$TF_ENV" =~ ^[a-z0-9-]+$ ]] || { echo "::error::invalid environment name" >&2; exit 2; }

scripts_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root="$(git rev-parse --show-toplevel)/infra/envs/$TF_ENV"
mkdir -p "$OUT_DIR"

exitcode=0
if [[ "${ASSUME_ROLE_OUTCOME:-success}" != success ]]; then
  exitcode=1
  {
    echo "Could not assume the plan role ${PLAN_ROLE:-} via GitHub OIDC."
    echo "See the 'Assume the read-only plan role' step log. Usual causes: the AWS bootstrap"
    echo "(infra/bootstrap/<env>) has not been applied yet, or the role's trust policy does not"
    echo "match this run's OIDC subject."
  } >"$OUT_DIR/plan.log"
elif ! printf '%s' "${TF_CI_TFVARS:?}" >"$root/ci.auto.tfvars.json" ||
  ! terraform -chdir="$root" init -input=false -lockfile=readonly -no-color \
    -backend-config="bucket=${TF_STATE_BUCKET:?}" >"$OUT_DIR/plan.log" 2>&1; then
  exitcode=1
else
  mode_args=()
  [[ "${PLAN_MODE:-normal}" == refresh-only ]] && mode_args=(-refresh-only)
  terraform -chdir="$root" plan "${mode_args[@]}" -lock=false -input=false -no-color -detailed-exitcode \
    -out="$OUT_DIR/tfplan" >>"$OUT_DIR/plan.log" 2>&1 || exitcode=$?
fi

if [[ $exitcode == 0 || $exitcode == 2 ]]; then
  terraform -chdir="$root" show -no-color "$OUT_DIR/tfplan" >"$OUT_DIR/plan.txt"
  terraform -chdir="$root" show -json "$OUT_DIR/tfplan" >"$OUT_DIR/plan.json"
fi
echo "$exitcode" >"$OUT_DIR/exitcode"

export PLAN_EXITCODE=$exitcode PLAN_TEXT="$OUT_DIR/plan.txt" PLAN_JSON="$OUT_DIR/plan.json" PLAN_LOG="$OUT_DIR/plan.log"
MAX_CHARS=60000 "$scripts_dir/render-plan.sh" >"$OUT_DIR/comment.md"
MAX_CHARS=900000 "$scripts_dir/render-plan.sh" >"$OUT_DIR/summary.md"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "exitcode=$exitcode"
    echo "has_changes=$([[ $exitcode == 2 ]] && echo true || echo false)"
    echo "auto_safe=$([[ $exitcode == 2 ]] && "$scripts_dir/plan-is-safe.sh" "$OUT_DIR/plan.json" || echo false)"
  } >>"$GITHUB_OUTPUT"
fi
echo "terraform plan for $TF_ENV finished with exit code $exitcode"
