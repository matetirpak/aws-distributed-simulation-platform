#!/usr/bin/env bash
# The review rule of the apply-time merge gate (apply-preflight.sh). The main
# ruleset enforces nearly the same rule before merging (docs/ci-cd.md).
# Source it, then:
#
#   review_decision PR_NUMBER CI_JSON   ->  prints "ok|<detail>" or "bad|<reason>"
#
# A PR passes when, on its final commit, someone other than its author approved
# it, or its author is an approver and CI_JSON sets allow_self_review; and nobody
# has an open change request. Needs gh (GH_TOKEN), jq and GITHUB_REPOSITORY.
# shellcheck source=.github/scripts/gh-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/gh-retry.sh"

review_decision() {
  local number=$1 cfg=$2 repo=${GITHUB_REPOSITORY:?} pr author head requested decision author_id
  pr=$(gh_retry api "repos/$repo/pulls/$number") || { echo "bad|could not read PR #$number from GitHub"; return; }
  author=$(jq -r .user.login <<<"$pr")
  head=$(jq -r .head.sha <<<"$pr")
  requested=$(jq -r '[.requested_reviewers[].login] | join(", ")' <<<"$pr")

  decision=$(gh_retry api --paginate "repos/$repo/pulls/$number/reviews" | jq -s --arg author "$author" --arg head "$head" '
    (add // []) as $all
    | ([$all[] | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")]
       | group_by(.user.login) | map(max_by(.submitted_at))) as $latest
    | { approved: [$latest[] | select(.state == "APPROVED" and .user.login != $author and .commit_id == $head) | .user.login],
        stale: [$latest[] | select(.state == "APPROVED" and .user.login != $author and .commit_id != $head) | "\(.user.login) (on \(.commit_id[0:7]))"],
        blocking: [$latest[] | select(.state == "CHANGES_REQUESTED") | .user.login],
        dismissed: [$latest[] | select(.state == "DISMISSED") | .user.login],
        comments: ([$all[] | select(.state == "COMMENTED")] | length) }') || { echo "bad|could not read the reviews of PR #$number"; return; }

  if [[ -n "$(jq -r '.blocking | join(", ")' <<<"$decision")" ]]; then
    echo "bad|PR #$number has an open change request from $(jq -r '.blocking | join(", ")' <<<"$decision"); they must approve, or their review must be dismissed"
  elif [[ -n "$(jq -r '.approved | join(", ")' <<<"$decision")" ]]; then
    echo "ok|PR #$number by $author, approved on its final commit by $(jq -r '.approved | join(", ")' <<<"$decision")"
  elif author_id=$(gh_retry api "users/$author" --jq .id) &&
    jq -e --argjson id "$author_id" '(.allow_self_review // false) == true and ((.approver_ids // []) | index($id) != null)' "$cfg" >/dev/null; then
    echo "ok|PR #$number by approver $author (self-review allowed by $(basename "$(dirname "$cfg")")'s ci.json)"
  elif [[ -n "$(jq -r '.stale | join(", ")' <<<"$decision")" ]]; then
    echo "bad|PR #$number by $author was approved by $(jq -r '.stale | join(", ")' <<<"$decision"), but commits were pushed afterwards (final commit ${head:0:7}); the approval must be on the final commit, so approve again"
  elif [[ -n "$(jq -r '.dismissed | join(", ")' <<<"$decision")" ]]; then
    echo "bad|PR #$number by $author: the approval by $(jq -r '.dismissed | join(", ")' <<<"$decision") was dismissed; approve again"
  elif [[ "$(jq -r .comments <<<"$decision")" != 0 ]]; then
    echo "bad|PR #$number by $author only has comment reviews; nobody clicked Approve"
  else
    echo "bad|PR #$number by $author has no review at all${requested:+ (requested from $requested, never submitted)}. Merging is not approving: a reviewer must submit Approve (Files changed → Review changes → Approve)"
  fi
}
