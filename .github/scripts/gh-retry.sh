#!/usr/bin/env bash
# Retry helper for GitHub CLI calls, which occasionally fail with transient
# server errors ("GraphQL: Something went wrong ...", "unexpected end of JSON
# input"). Source it:   source .github/scripts/gh-retry.sh
#
#   gh_retry api repos/...   # for reads and idempotent writes (edit, close, PATCH)
#   retry_step FUNCTION ...  # re-runs a whole find-then-create step, so a create
#                            # that "failed" but actually succeeded isn't duplicated
#   label_exists NAME        # whether the repository has that label
#
# Five attempts, waiting 5, 10, 20 and 40 seconds in between (75 s at most), because
# GitHub's API can return empty responses for longer than a few seconds. Inside a
# retry_step, gh_retry tries only once, because the step as a whole is retried: nested
# retries would multiply the wait past the jobs' 5-minute timeouts.

retry_delays=(5 10 20 40)

gh_retry() {
  local attempt out attempts=5
  [[ -z ${in_retry_step:-} ]] || attempts=1
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if out=$(gh "$@"); then
      [[ -z "$out" ]] || printf '%s\n' "$out"
      return 0
    fi
    echo "gh $1 failed (attempt $attempt of $attempts)" >&2
    ((attempt == attempts)) || sleep "${retry_delays[attempt - 1]}"
  done
  return 1
}

retry_step() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if in_retry_step=1 "$@"; then
      return 0
    fi
    echo "$1 failed (attempt $attempt of 5)" >&2
    ((attempt == 5)) || sleep "${retry_delays[attempt - 1]}"
  done
  return 1
}

label_exists() {
  gh_retry api "repos/${GH_REPO:-$GITHUB_REPOSITORY}/labels/$1" --silent 2>/dev/null
}
