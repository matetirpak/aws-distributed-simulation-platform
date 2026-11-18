#!/usr/bin/env bash
# Record a successful apply as a GitHub deployment of ENV at SHA. The merge gate starts
# from the latest one (apply-preflight.sh), whether it came from apply.yml or from the
# auto-apply after a merge. Shows up under the repository's Deployments.
#
#   record-apply.sh ENV SHA LOG_URL
#
# Needs gh (GH_TOKEN with deployments: write) and GITHUB_REPOSITORY.
set -euo pipefail
# shellcheck source=.github/scripts/gh-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/gh-retry.sh"

env_name=$1 sha=$2 log_url=$3
: "${GITHUB_REPOSITORY:?}"
[[ "$env_name" =~ ^[a-z0-9-]+$ && "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::invalid environment or sha" >&2; exit 2; }

# A file, not stdin: gh_retry may run the request more than once. A retried create can
# record the same commit twice, which is harmless: the gate only reads the latest commit.
request=$(mktemp)
jq -n --arg ref "$sha" --arg env "$env_name" '{
  ref: $ref, environment: $env, auto_merge: false, required_contexts: [],
  production_environment: ($env == "prod"), description: "terraform apply"
}' >"$request"
id=$(gh_retry api -X POST "repos/$GITHUB_REPOSITORY/deployments" --input "$request" --jq .id)
gh_retry api -X POST "repos/$GITHUB_REPOSITORY/deployments/$id/statuses" \
  -f state=success -f log_url="$log_url" -F auto_inactive=true --silent
echo "Recorded the apply of ${sha:0:7} as deployment $id ($env_name)."
