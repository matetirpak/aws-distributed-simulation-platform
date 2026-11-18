#!/usr/bin/env bash
# Keep one "Plan awaiting approval: <env>" issue in sync with the latest plan on main.
#
#   approval-issue.sh open  ENV PLAN_RUN_ID COMMIT_SHA PLAN_MARKDOWN_FILE [REASON]
#   approval-issue.sh close ENV MESSAGE
#
# Issues are matched by exact title, so one without the label is still found.
# Needs GH_TOKEN (issues: write), GH_REPO and GITHUB_SERVER_URL.
set -euo pipefail
# shellcheck source=.github/scripts/gh-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/gh-retry.sh"

action=$1 env_name=$2
: "${GH_REPO:?}" "${GITHUB_SERVER_URL:?}"
[[ "$env_name" =~ ^[a-z0-9-]+$ ]] || { echo "::error::invalid environment name" >&2; exit 2; }

title="Plan awaiting approval: $env_name"
find_issue() {
  gh_retry issue list --state open --limit 200 --json number,title |
    jq -r --arg t "$title" '[.[] | select(.title == $t)] | first | .number // empty'
}
existing=$(find_issue)

case "$action" in
  open)
    run_id=$3 sha=$4 plan_md=$5 reason=${6:-}
    [[ "$run_id" =~ ^[0-9]+$ && "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::invalid run id or sha" >&2; exit 2; }
    run_url="$GITHUB_SERVER_URL/$GH_REPO/actions/runs/$run_id"
    body=$(mktemp)
    {
      echo "**[Deploy run $run_id]($run_url) planned commit \`${sha:0:7}\` for \`$env_name\`. Nothing is applied until an approver approves it.**"
      echo
      if [[ -n "$reason" ]]; then
        echo "**Not applied automatically:** $reason"
        echo
      fi
      echo "1. Read the plan below (also in the [run summary]($run_url))."
      echo "2. Approve by running:"
      echo '   ```bash'
      echo "   gh workflow run apply.yml --ref main -f environment=$env_name -f plan_run_id=$run_id"
      echo '   ```'
      echo "   or [run the apply workflow]($GITHUB_SERVER_URL/$GH_REPO/actions/workflows/apply.yml) on \`main\` with plan_run_id \`$run_id\`."
      echo
      echo "This issue is updated when a newer commit produces a new plan, and closed once a plan is applied. Docs: \`docs/ci-cd.md\`."
      echo
      echo "---"
      echo
      cat "$plan_md"
    } >"$body"
    if [[ -n "$existing" ]]; then
      gh_retry issue edit "$existing" --body-file "$body" >/dev/null
      gh_retry issue comment "$existing" --body "New plan for \`${sha:0:7}\` ([deploy run $run_id]($run_url)) replaces the previous one; only this one can be applied." >/dev/null
      echo "Updated #$existing ($title)"
    else
      label_args=(--label awaiting-approval)
      if ! label_exists awaiting-approval; then
        echo "::warning::Label 'awaiting-approval' does not exist (create it, see docs/setup.md); opening the issue without it."
        label_args=()
      fi
      # Retried as find-then-create, so an attempt that errored but went through
      # isn't duplicated. A failed find fails the attempt rather than counting as
      # "no issue yet".
      open_issue() {
        local found
        found=$(find_issue) || return 1
        [[ -z "$found" ]] || return 0
        gh issue create --title "$title" "${label_args[@]}" --body-file "$body"
      }
      retry_step open_issue
      echo "Opened: $title"
    fi
    ;;
  close)
    message=$3
    if [[ -n "$existing" ]]; then
      gh_retry issue close "$existing" --comment "$message" >/dev/null
      echo "Closed #$existing ($title)"
    else
      echo "No open '$title' issue."
    fi
    ;;
  *)
    echo "usage: $0 open ENV RUN_ID SHA FILE [REASON] | close ENV MESSAGE" >&2
    exit 2
    ;;
esac
