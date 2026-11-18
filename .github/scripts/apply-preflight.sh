#!/usr/bin/env bash
# Checks that run before a saved plan is applied: by apply.yml (APPLY_MODE=manual, the
# default) or by the auto-apply job right after a merge (APPLY_MODE=auto). Every check
# fails closed with a message that says what to do instead.
#
#   1. the person who triggered this run is an approver for the environment
#   2. the plan run is a successful deploy.yml run on main
#   3. that plan was made from the current main HEAD, which is also the commit
#      this apply run is executing
#   4. merge gate: every commit on main since the last apply of this environment
#      came from a merged PR that someone other than its author approved on its
#      final commit (or whose author is an approver, if ci.json sets
#      allow_self_review), with no outstanding change requests and a green
#      ci-result. Unreviewed commits need acknowledge_unreviewed=true.
#      (A second layer behind the main ruleset, which owners can switch off and
#      infra-approvers can bypass.)
#   5. environments listed in ci.json "apply_after" were applied for this commit
#
# Auto mode skips 1 and 2: the plan comes from this same run, and the approval is the
# reviewed PR (4). Nothing in auto mode is acknowledged: where a check needs a human, the
# apply is skipped (exit 0, skip=true) and the plan waits for an approver as usual.
#
# Env: TF_ENV, PLAN_RUN_ID, TRIGGERING_ACTOR, GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SHA,
#      ACKNOWLEDGE_UNREVIEWED (true/false), APPLY_MODE (manual/auto).
#      Needs a full-history checkout (fetch-depth: 0).
# Outputs (GITHUB_OUTPUT): pr_number (the PR behind HEAD, if any); skip (true/false),
#      skip_kind (superseded: a newer commit's run applies it; needs_approval) and reason.
set -euo pipefail

: "${TF_ENV:?}" "${PLAN_RUN_ID:?}" "${TRIGGERING_ACTOR:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_SHA:?}"
repo=$GITHUB_REPOSITORY
summary=${GITHUB_STEP_SUMMARY:-/dev/null}

fail() {
  echo "::error::$*"
  echo "- ❌ $*" >>"$summary"
  exit 1
}
ok() {
  echo "✓ $*"
  echo "- ✅ $*" >>"$summary"
}
mode=${APPLY_MODE:-manual}
[[ "$mode" == manual || "$mode" == auto ]] || fail "APPLY_MODE must be manual or auto."
skip() { # KIND REASON: auto mode only; the plan is left to an approver
  echo "::notice::Not applied automatically: $2"
  echo "- ⏸️ Not applied automatically: $2" >>"$summary"
  printf 'skip=true\nskip_kind=%s\nreason=%s\n' "$1" "${2//$'\n'/ }" >>"${GITHUB_OUTPUT:-/dev/stdout}"
  exit 0
}

echo "### Pre-flight checks" >>"$summary"

[[ "$TF_ENV" =~ ^[a-z0-9-]+$ ]] || fail "Invalid environment name."
[[ "$PLAN_RUN_ID" =~ ^[0-9]+$ ]] || fail "plan_run_id must be the numeric ID of a deploy run."
[[ "$TRIGGERING_ACTOR" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ]] || fail "Unexpected actor name."
cfg="$(git rev-parse --show-toplevel)/infra/envs/$TF_ENV/ci.json"
[[ -f "$cfg" ]] || fail "No ci.json for environment $TF_ENV."

if [[ "$mode" == manual ]]; then
  # 1. Approver. The apply role's trust policy enforces the same list (actor_id);
  # this check gives a clear message early and also covers re-runs started by
  # someone else (a re-run keeps the original actor in the OIDC token).
  actor_id=$(gh api "users/$TRIGGERING_ACTOR" --jq .id) || fail "Could not look up GitHub user $TRIGGERING_ACTOR."
  if ! jq -e --argjson id "$actor_id" '(.approver_ids // []) | index($id) != null' "$cfg" >/dev/null; then
    fail "$TRIGGERING_ACTOR is not an approver for $TF_ENV (approver_ids in ci.json)."
  fi
  ok "$TRIGGERING_ACTOR is an approver for $TF_ENV"

  # 2. The plan run.
  run=$(gh api "repos/$repo/actions/runs/$PLAN_RUN_ID") || fail "Deploy run $PLAN_RUN_ID not found. Use the run ID from the deploy run's URL."
  path=$(jq -r '.path | sub("@.*$"; "")' <<<"$run")
  branch=$(jq -r .head_branch <<<"$run")
  event=$(jq -r .event <<<"$run")
  status=$(jq -r .status <<<"$run")
  conclusion=$(jq -r .conclusion <<<"$run")
  plan_sha=$(jq -r .head_sha <<<"$run")
  [[ "$path" == .github/workflows/deploy.yml ]] || fail "Run $PLAN_RUN_ID is not a deploy.yml run ($path)."
  [[ "$branch" == main ]] || fail "Run $PLAN_RUN_ID did not run on main ($branch)."
  [[ "$event" == push || "$event" == workflow_dispatch ]] || fail "Run $PLAN_RUN_ID was triggered by '$event'."
  [[ "$status" == completed && "$conclusion" == success ]] || fail "Deploy run $PLAN_RUN_ID is $status/$conclusion, not completed/success."
  ok "Plan comes from successful deploy run $PLAN_RUN_ID on main"
else
  [[ "$PLAN_RUN_ID" == "${GITHUB_RUN_ID:-}" ]] || fail "Auto mode applies only the plan of its own run."
  plan_sha=$GITHUB_SHA
  ok "Auto-apply of this run's plan: the approval is the reviewed PR (merge gate below)"
fi

# 3. Freshness.
main_sha=$(gh api "repos/$repo/commits/main" --jq .sha)
if [[ "$mode" == auto && "$main_sha" != "$GITHUB_SHA" ]]; then
  skip superseded "main moved on to ${main_sha:0:7}; that commit's deploy run applies this change as well."
fi
if [[ "$plan_sha" != "$main_sha" ]]; then
  fail "A newer commit is on main: apply its plan instead. (Plan is for ${plan_sha:0:7}, main is at ${main_sha:0:7}.)"
fi
if [[ "$GITHUB_SHA" != "$main_sha" ]]; then
  fail "This apply run started at ${GITHUB_SHA:0:7} but main is now at ${main_sha:0:7}. Apply the plan for the new commit instead."
fi
ok "Plan is for the current main HEAD ${main_sha:0:7}"

# 4. Merge gate. An apply includes every change merged since the last apply of
# this environment, so every first-parent commit in that range is checked, not
# only HEAD. Unreviewed commits are refused unless the approver re-runs with
# acknowledge_unreviewed=true after reading them (a deliberate, logged decision;
# without it, one unreviewed commit would block every later apply).
# shellcheck source=.github/scripts/review-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/review-lib.sh"

review_commit() { # SHA -> "ok|<pr>|<detail>" or "bad|<pr or ->|<problem>"
  local sha=$1 pr number pr_head verdict detail ci_result
  pr=$(gh api "repos/$repo/commits/$sha/pulls" |
    jq -c --arg sha "$sha" '[.[] | select(.merged_at != null and .base.ref == "main" and .merge_commit_sha == $sha)] | first // empty')
  if [[ -z "$pr" ]]; then
    echo "bad|-|${sha:0:7} is not the merge commit of a merged PR (direct push?)"
    return
  fi
  number=$(jq -r .number <<<"$pr")
  pr_head=$(jq -r .head.sha <<<"$pr")

  # Nearly the rule the main ruleset enforces before merging (docs/ci-cd.md).
  IFS='|' read -r verdict detail < <(review_decision "$number" "$cfg")
  if [[ "$verdict" != ok ]]; then
    echo "bad|$number|${detail:-could not evaluate the reviews of PR #$number}"
    return
  fi
  ci_result=$(gh api "repos/$repo/commits/$pr_head/check-runs?check_name=ci-result" |
    jq -r '[.check_runs[]] | sort_by(.started_at) | last | .conclusion // "missing"')
  if [[ "$ci_result" != success ]]; then
    echo "bad|$number|PR #$number's ci-result on its final commit is '$ci_result'"
    return
  fi
  echo "ok|$number|$detail, ci-result green"
}

# The last apply is the latest deployment record-apply.sh made (apply.yml and auto-apply
# both record one). Before the first exists, fall back to the last successful apply.yml run.
last_applied=$(gh api "repos/$repo/deployments?environment=$TF_ENV&per_page=1" --jq '.[0].sha // empty') ||
  fail "Could not read the deployments of $TF_ENV."
if [[ -z "$last_applied" ]]; then
  last_applied=$(gh api "repos/$repo/actions/workflows/apply.yml/runs?branch=main&status=success&per_page=100" |
    jq -r --arg p "apply $TF_ENV (" '[.workflow_runs[] | select(.display_title | startswith($p))] | first | .head_sha // empty')
fi
problems=()
if [[ -z "$last_applied" ]]; then
  commits=("$GITHUB_SHA")
  range="HEAD only (first apply of $TF_ENV)"
elif git merge-base --is-ancestor "$last_applied" "$GITHUB_SHA" 2>/dev/null; then
  mapfile -t commits < <(git rev-list --first-parent "$last_applied..$GITHUB_SHA")
  range="${#commits[@]} commit(s) since the last apply of $TF_ENV (${last_applied:0:7})"
else
  commits=("$GITHUB_SHA")
  range="HEAD only: the last applied commit ${last_applied:0:7} is not an ancestor of main (history rewritten?)"
  problems+=("main's history was rewritten since the last apply (${last_applied:0:7}); review the difference yourself")
fi

number=""
for c in "${commits[@]}"; do
  IFS='|' read -r verdict pr detail < <(review_commit "$c") || true
  [[ "$verdict" == ok || "$verdict" == bad ]] || fail "Could not check commit ${c:0:7} (GitHub API error)."
  [[ "$c" == "$GITHUB_SHA" && "$pr" != - ]] && number=$pr
  if [[ "$verdict" == ok ]]; then
    echo "  ${c:0:7}: $detail"
  else
    problems+=("$detail")
  fi
done

if ((${#problems[@]} == 0)); then
  ok "Merge gate: all $range came from reviewed PRs with a green ci-result"
else
  for p in "${problems[@]}"; do echo "- ⚠️ $p" >>"$summary"; echo "::warning::$p"; done
  if [[ "$mode" == auto ]]; then
    skip needs_approval "Merge gate: ${#problems[@]} unreviewed change(s) in $range: $(printf '%s; ' "${problems[@]}")an approver reads them and applies with acknowledge_unreviewed=true."
  elif [[ "${ACKNOWLEDGE_UNREVIEWED:-false}" == true ]]; then
    ok "Merge gate: ${#problems[@]} unreviewed change(s) in $range ACKNOWLEDGED by $TRIGGERING_ACTOR (acknowledge_unreviewed=true)"
  else
    fail "Merge gate: ${#problems[@]} unreviewed change(s) in $range: $(printf '%s; ' "${problems[@]}")Read them in the diff (a revert through a reviewed PR is fine too), then re-run apply.yml with acknowledge_unreviewed=true."
  fi
fi

# 5. Ordering between environments (e.g. prod after dev).
for prereq in $(jq -r '(.apply_after // [])[]' "$cfg"); do
  [[ "$prereq" =~ ^[a-z0-9-]+$ ]] || fail "Invalid apply_after entry in ci.json."
  applied=$(gh api "repos/$repo/deployments?environment=$prereq&sha=$GITHUB_SHA&per_page=1" --jq length) ||
    fail "Could not read the deployments of $prereq."
  if ((applied == 0)); then
    [[ "$mode" == auto ]] && skip needs_approval "$prereq isn't applied for ${GITHUB_SHA:0:7} yet (apply_after in ci.json)."
    fail "Apply $prereq for ${GITHUB_SHA:0:7} first (apply_after in ci.json)."
  fi
  ok "$prereq was already applied for ${GITHUB_SHA:0:7}"
done

printf 'pr_number=%s\nskip=false\n' "$number" >>"${GITHUB_OUTPUT:-/dev/stdout}"
