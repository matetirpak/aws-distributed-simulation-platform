#!/usr/bin/env bash
# Render a Terraform plan as Markdown for a PR comment or a job summary.
#
# Inputs (environment variables):
#   TF_ENV         environment name (required, [a-z0-9-]+)
#   PLAN_EXITCODE  exit code of `terraform plan -detailed-exitcode` (0, 2, or anything else = failed)
#   PLAN_TEXT      file with `terraform show -no-color tfplan`   (needed for exit 0/2)
#   PLAN_JSON      file with `terraform show -json tfplan`       (needed for exit 0/2)
#   PLAN_LOG       file with the plan's stdout/stderr            (shown when the plan failed)
#   COMMIT_SHA     commit the plan was made from
#   RUN_URL        link to the workflow run
#   EXTRA_MD       optional Markdown file inserted below the summary (e.g. how to approve)
#   MAX_CHARS      body size limit (default 60000; GitHub's comment limit is 65536)
#   MARKER_NAME    sticky-comment marker prefix (default terraform-plan)
#   PLAN_MODE      "normal" (default) or "refresh-only" (drift: reads resource_drift)
#
# Writes Markdown to stdout. Plan output is treated strictly as data: it is only
# ever read from files, never interpolated into a script.
# shellcheck disable=SC2016 # backticks in single quotes are literal Markdown, not shell
set -euo pipefail
export LC_ALL=C.UTF-8

: "${TF_ENV:?}" "${PLAN_EXITCODE:?}" "${COMMIT_SHA:?}" "${RUN_URL:?}"
[[ "$TF_ENV" =~ ^[a-z0-9-]+$ ]] || { echo "invalid TF_ENV" >&2; exit 2; }
max_chars=${MAX_CHARS:-60000}
marker_name=${MARKER_NAME:-terraform-plan}
plan_mode=${PLAN_MODE:-normal}
[[ "$plan_mode" == normal || "$plan_mode" == refresh-only ]] || { echo "invalid PLAN_MODE" >&2; exit 2; }
title="Terraform plan"
[[ "$plan_mode" == refresh-only ]] && title="Terraform drift"
[[ "$marker_name" =~ ^[a-z0-9-]+$ ]] || { echo "invalid MARKER_NAME" >&2; exit 2; }

marker="<!-- ${marker_name}:${TF_ENV} -->"
meta="Commit \`${COMMIT_SHA:0:7}\` · [workflow run](${RUN_URL})"

# Fence that cannot be closed by backticks inside the content.
fence_for() {
  local longest
  longest=$(grep -o '`\+' "$1" 2>/dev/null | awk '{ if (length > m) m = length } END { print m + 0 }')
  printf '%*s' $((longest >= 3 ? longest + 1 : 3)) '' | tr ' ' '`'
}

code_span() { printf '`%s`' "${1//\`/\'}"; }

extra=""
if [[ -n "${EXTRA_MD:-}" && -s "${EXTRA_MD}" ]]; then
  extra=$(<"$EXTRA_MD")$'\n\n'
fi

if [[ "$PLAN_EXITCODE" != 0 && "$PLAN_EXITCODE" != 2 ]]; then
  log=$(mktemp)
  if [[ -n "${PLAN_LOG:-}" && -f "$PLAN_LOG" ]]; then
    tail -n 50 "$PLAN_LOG" >"$log"
  else
    echo "(no log captured)" >"$log"
  fi
  fence=$(fence_for "$log")
  printf '%s\n### ❌ %s: `%s`\n\n%s · **plan failed** (exit code %s)\n\n%sLast 50 lines of output:\n\n%s\n%s\n%s\n' \
    "$marker" "$title" "$TF_ENV" "$meta" "$PLAN_EXITCODE" "$extra" "$fence" "$(<"$log")" "$fence"
  exit 0
fi

: "${PLAN_TEXT:?}" "${PLAN_JSON:?}"

section() { # heading, addresses...
  local heading=$1 out
  shift
  (($# > 0)) || return 0
  out=$'\n\n'"**${heading}:**"$'\n'
  for a in "$@"; do out+="- $(code_span "$a")"$'\n'; done
  printf '%s' "$out"
}

if [[ "$plan_mode" == refresh-only ]]; then
  # Refresh-only plans report changes made outside Terraform in resource_drift.
  mapfile -t drifted < <(jq -r '.resource_drift[]? | select(.change.actions == ["update"]) | .address' "$PLAN_JSON")
  mapfile -t vanished < <(jq -r '.resource_drift[]? | select(.change.actions == ["delete"]) | .address' "$PLAN_JSON")
  if [[ "$PLAN_EXITCODE" == 0 ]]; then
    icon="✅" status="no drift"
  else
    icon="⚠️" status="changed outside Terraform"
  fi
  summary="**Drift:** ${#drifted[@]} changed, ${#vanished[@]} deleted outside Terraform"
  risky="$(section "Deleted outside Terraform" "${vanished[@]}")$(section "Changed outside Terraform" "${drifted[@]}")"
else
  read -r n_add n_change n_destroy n_replace n_forget < <(jq -r '
    [.resource_changes[]?.change.actions] as $a
    | [ ($a | map(select(. == ["create"])) | length),
        ($a | map(select(. == ["update"])) | length),
        ($a | map(select(. == ["delete"])) | length),
        ($a | map(select(. == ["delete", "create"] or . == ["create", "delete"])) | length),
        ($a | map(select(. == ["forget"])) | length) ]
    | @tsv' "$PLAN_JSON")

  mapfile -t destroyed < <(jq -r '.resource_changes[]? | select(.change.actions == ["delete"]) | .address' "$PLAN_JSON")
  mapfile -t replaced < <(jq -r '.resource_changes[]? | select(.change.actions == ["delete", "create"] or .change.actions == ["create", "delete"]) | .address' "$PLAN_JSON")

  if [[ "$PLAN_EXITCODE" == 0 ]]; then
    icon="✅" status="no changes"
  elif ((n_destroy + n_replace > 0)); then
    icon="⚠️" status="includes destroy or replace"
  elif ((n_add + n_change + n_forget == 0)); then
    icon="📝" status="changes (outputs only)"
  else
    icon="📝" status="changes"
  fi

  summary="**Plan:** ${n_add} to add, ${n_change} to change, ${n_destroy} to destroy, ${n_replace} to replace"
  ((n_forget > 0)) && summary+=", ${n_forget} to remove from state"
  risky="$(section "Destroyed" "${destroyed[@]}")$(section "Replaced (destroy and re-create)" "${replaced[@]}")"
fi

# Move a leading +, - or ~ to column 0 so GitHub's diff highlighting colours it.
diff_text=$(sed -E 's/^([[:space:]]+)([-+~])/\2\1/' "$PLAN_TEXT")
fence=$(fence_for "$PLAN_TEXT")

prefix=$(printf '%s\n### %s %s: `%s`\n\n%s · **%s**\n\n%s%s\n\n%s<details><summary>Full plan</summary>\n\n%sdiff\n' \
  "$marker" "$icon" "$title" "$TF_ENV" "$meta" "$status" "$summary" "$risky" "$extra" "$fence")
suffix=$(printf '\n%s\n\n</details>' "$fence")
note=$'\n\n> **Truncated:** the plan is too long for a comment. The full plan is in the [workflow run summary]('"${RUN_URL}"').'

budget=$((max_chars - ${#prefix} - ${#suffix} - ${#note} - 16))
if ((${#diff_text} > budget)); then
  ((budget > 0)) || budget=0
  diff_text=${diff_text:0:budget}
  diff_text=${diff_text%$'\n'*}$'\n… (truncated)'
else
  note=""
fi

printf '%s%s%s%s\n' "$prefix" "$diff_text" "$suffix" "$note"
