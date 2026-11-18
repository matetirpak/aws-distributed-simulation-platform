#!/usr/bin/env bash
# Create or update a sticky issue/PR comment identified by a hidden marker.
#
#   upsert-comment.sh NUMBER BODY_FILE
#
# The first line of BODY_FILE must be the marker, e.g. <!-- terraform-plan:dev -->.
# Only comments written by github-actions[bot] are considered, so nobody can
# hijack the sticky comment by posting the marker themselves.
# Needs GH_TOKEN (pull-requests: write or issues: write) and GITHUB_REPOSITORY.
set -euo pipefail
# shellcheck source=.github/scripts/gh-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/gh-retry.sh"

number=$1 body_file=$2
: "${GITHUB_REPOSITORY:?}"
[[ "$number" =~ ^[0-9]+$ ]] || { echo "::error::invalid issue number" >&2; exit 2; }
marker=$(head -n 1 "$body_file")
marker_re='^<!-- [a-z0-9:-]+ -->$'
[[ "$marker" =~ $marker_re ]] || { echo "::error::body must start with a marker comment" >&2; exit 2; }
payload=$(jq -Rs '{body: .}' "$body_file")

# Find, then update or create. Retried as a whole, so a create that reported an
# error but went through is found (and updated) instead of duplicated.
upsert() {
  local ids existing
  ids=$(gh api --paginate "repos/$GITHUB_REPOSITORY/issues/$number/comments" |
    jq -r --arg m "$marker" '.[] | select(.user.login == "github-actions[bot]" and (.body | startswith($m))) | .id') || return 1
  existing=${ids%%$'\n'*}
  if [[ -n "$existing" ]]; then
    gh api -X PATCH "repos/$GITHUB_REPOSITORY/issues/comments/$existing" --input - <<<"$payload" >/dev/null || return 1
    echo "Updated comment $existing on #$number"
  else
    gh api -X POST "repos/$GITHUB_REPOSITORY/issues/$number/comments" --input - <<<"$payload" >/dev/null || return 1
    echo "Created comment on #$number"
  fi
}
retry_step upsert
