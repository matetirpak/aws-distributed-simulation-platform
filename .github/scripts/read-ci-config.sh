#!/usr/bin/env bash
# Read and validate one environment's CI config.
#
#   CI_VARS='${{ toJSON(vars) }}' read-ci-config.sh ENV plan_role_arn|apply_role_arn
#
# Two sources, so that no account details are committed:
#   infra/envs/<env>/ci.json    region, auto_apply, approvers (committed)
#   repository variable INFRA_<ENV>, JSON with plan_role_arn, apply_role_arn, state_bucket and
#   tfvars, the root's account-specific inputs (docs/ci-cd.md)
#
# Writes role, region, account and auto_apply to $GITHUB_OUTPUT (or stdout outside Actions), and
# TF_STATE_BUCKET and TF_CI_TFVARS (tfvars plus account_id and repository) to $GITHUB_ENV for
# tf-plan.sh and tf-apply.sh.
set -euo pipefail

env_name=$1 key=$2
[[ "$env_name" =~ ^[a-z0-9-]+$ ]] || { echo "::error::invalid environment name" >&2; exit 2; }
[[ "$key" == plan_role_arn || "$key" == apply_role_arn ]] || { echo "::error::invalid key $key" >&2; exit 2; }

cfg="$(git rev-parse --show-toplevel)/infra/envs/$env_name/ci.json"
[[ -f "$cfg" ]] || { echo "::error::$cfg not found" >&2; exit 1; }
var_name="INFRA_${env_name^^}"
var_name=${var_name//-/_}
account_cfg=$(jq -ec --arg k "$var_name" '.[$k] // empty | fromjson' <<<"${CI_VARS:-{\}}") ||
  { echo "::error::repository variable $var_name is missing or not JSON (docs/ci-cd.md)" >&2; exit 1; }

role=$(jq -er --arg k "$key" '.[$k]' <<<"$account_cfg")
state_bucket=$(jq -er .state_bucket <<<"$account_cfg")
region=$(jq -er .aws_region "$cfg")
[[ "$role" =~ ^arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+$ ]] || { echo "::error::bad $key in $var_name" >&2; exit 1; }
account=${BASH_REMATCH[1]}
[[ "$state_bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || { echo "::error::bad state_bucket in $var_name" >&2; exit 1; }
[[ "$region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || { echo "::error::bad aws_region in $cfg" >&2; exit 1; }
auto_apply=$(jq -r '.auto_apply // false' "$cfg")
[[ "$auto_apply" == true || "$auto_apply" == false ]] || { echo "::error::auto_apply in $cfg must be true or false" >&2; exit 1; }
tfvars=$(jq -c --arg account "$account" --arg repository "${GITHUB_REPOSITORY:-local}" \
  '(.tfvars // {}) + {account_id: $account, repository: $repository}' <<<"$account_cfg")

printf 'role=%s\nregion=%s\naccount=%s\nauto_apply=%s\n' "$role" "$region" "$account" "$auto_apply" >>"${GITHUB_OUTPUT:-/dev/stdout}"
printf 'TF_STATE_BUCKET=%s\nTF_CI_TFVARS=%s\n' "$state_bucket" "$tfvars" >>"${GITHUB_ENV:-/dev/null}"
