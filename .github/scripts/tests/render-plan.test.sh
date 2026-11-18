#!/usr/bin/env bash
# Tests for render-plan.sh against real `terraform show` output (fixtures were
# generated offline with the built-in terraform_data resource).
#   bash .github/scripts/tests/render-plan.test.sh
# shellcheck disable=SC2016 # backticks in single quotes are literal Markdown, not shell
set -euo pipefail
export LC_ALL=C.UTF-8

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
render="$here/../render-plan.sh"
fx="$here/fixtures"
tmp=$(mktemp -d)
failures=0

run() { # fixture [extra env...]
  local f=$1; shift
  env TF_ENV=dev COMMIT_SHA=0123456789abcdef RUN_URL=https://github.com/o/r/actions/runs/1 \
    PLAN_EXITCODE="$(<"$fx/$f/exitcode")" PLAN_TEXT="$fx/$f/show.txt" PLAN_JSON="$fx/$f/show.json" \
    PLAN_LOG="$fx/$f/plan.log" "$@" bash "$render"
}
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
has() { if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (missing: $2)"; fi; }
lacks() { if grep -qF -- "$2" <<<"$1"; then fail "$3 (unexpected: $2)"; else pass "$3"; fi; }

out=$(run create)
if [[ $(head -1 <<<"$out") == '<!-- terraform-plan:dev -->' ]]; then pass "create: marker on first line"; else fail "create: marker on first line"; fi
has "$out" '### 📝 Terraform plan: `dev`' "create: changes status"
has "$out" 'Commit `0123456` · [workflow run](https://github.com/o/r/actions/runs/1)' "create: short sha and run link"
has "$out" '**Plan:** 2 to add, 0 to change, 0 to destroy, 0 to replace' "create: counts"
has "$out" '```diff' "create: diff fence"
has "$out" '+   resource "terraform_data" "alpha" {' "create: + moved to column 0"
lacks "$out" 'Destroyed:' "create: no destroy list"

out=$(run mixed)
has "$out" '### ⚠️ Terraform plan: `dev`' "mixed: destroy/replace status"
has "$out" '**Plan:** 1 to add, 2 to change, 1 to destroy, 1 to replace' "mixed: counts"
has "$out" '- `terraform_data.gone`' "mixed: destroyed address listed"
has "$out" '- `terraform_data.replaceme`' "mixed: replaced address listed"
before_fold=${out%%<details>*}
has "$before_fold" 'terraform_data.gone' "mixed: destroyed address above the fold"
has "$out" '(sensitive value)' "mixed: sensitive value masked"
lacks "$out" 'new-secret' "mixed: sensitive value not leaked"
has "$out" '~   resource "terraform_data" "changeme" {' "mixed: ~ moved to column 0"

out=$(run noop)
has "$out" '### ✅ Terraform plan: `dev`' "noop: no-changes status"
has "$out" '**Plan:** 0 to add, 0 to change, 0 to destroy, 0 to replace' "noop: counts"

out=$(run failed)
has "$out" '### ❌ Terraform plan: `dev`' "failed: failed status"
has "$out" 'exit code 1' "failed: exit code shown"
has "$out" 'variable "undeclared"' "failed: error excerpt shown"

out=$(run drift PLAN_MODE=refresh-only MARKER_NAME=terraform-drift)
if [[ $(head -1 <<<"$out") == '<!-- terraform-drift:dev -->' ]]; then pass "drift: drift marker"; else fail "drift: drift marker"; fi
has "$out" '### ⚠️ Terraform drift: `dev`' "drift: drift status"
has "$out" '**Drift:** 1 changed, 1 deleted outside Terraform' "drift: counts from resource_drift"
has "${out%%<details>*}" '- `local_file.vanished`' "drift: deleted address above the fold"
has "${out%%<details>*}" '- `local_file.kept`' "drift: changed address above the fold"
out=$(run noop PLAN_MODE=refresh-only)
has "$out" '### ✅ Terraform drift: `dev`' "drift: clean status"

printf 'Approve with: `gh workflow run apply.yml`\n' >"$tmp/extra.md"
out=$(run create EXTRA_MD="$tmp/extra.md")
has "${out%%<details>*}" 'Approve with:' "extra markdown above the fold"

# Oversize plan: must fit the comment limit and still close its fence.
mkdir -p "$tmp/big"
cp "$fx/create/show.json" "$fx/create/exitcode" "$tmp/big/"
for _ in $(seq 1 4000); do echo '      + input  = "0123456789012345678901234567890123456789"'; done >"$tmp/big/plan.txt"
out=$(env TF_ENV=dev COMMIT_SHA=abc RUN_URL=https://x PLAN_EXITCODE=2 PLAN_TEXT="$tmp/big/plan.txt" PLAN_JSON="$tmp/big/show.json" bash "$render")
if ((${#out} <= 60000)); then pass "oversize: ${#out} chars <= 60000"; else fail "oversize: ${#out} chars > 60000"; fi
has "$out" '**Truncated:**' "oversize: truncation note"
has "$out" $'```\n\n</details>' "oversize: fence closed before </details>"
out=$(env TF_ENV=dev COMMIT_SHA=abc RUN_URL=https://x PLAN_EXITCODE=2 PLAN_TEXT="$tmp/big/plan.txt" PLAN_JSON="$tmp/big/show.json" MAX_CHARS=900000 bash "$render")
lacks "$out" '**Truncated:**' "oversize: not truncated for the job summary"

# Backticks inside the plan must not break out of the code block.
printf '  + x = "```"\n' >"$tmp/big/plan.txt"
out=$(env TF_ENV=dev COMMIT_SHA=abc RUN_URL=https://x PLAN_EXITCODE=2 PLAN_TEXT="$tmp/big/plan.txt" PLAN_JSON="$tmp/big/show.json" bash "$render")
has "$out" '````diff' "backticks: longer fence"

if env TF_ENV='dev`x' COMMIT_SHA=abc RUN_URL=https://x PLAN_EXITCODE=0 bash "$render" >/dev/null 2>&1; then
  fail "rejects a malformed environment name"
else
  pass "rejects a malformed environment name"
fi

# plan-is-safe.sh decides what may be applied without an approver (auto_apply in ci.json).
safe="$here/../plan-is-safe.sh"
expect_safe() { # plan.json expected name
  local got
  got=$("$safe" "$1")
  if [[ "$got" == "$2" ]]; then pass "$3"; else fail "$3 (got $got)"; fi
}
expect_safe "$fx/create/show.json" true "plan-is-safe: creates only"
expect_safe "$fx/noop/show.json" true "plan-is-safe: no-op"
expect_safe "$fx/mixed/show.json" false "plan-is-safe: destroy and replace"
jq '.resource_changes[0].change.actions = ["forget"]' "$fx/create/show.json" >"$tmp/forget.json"
expect_safe "$tmp/forget.json" false "plan-is-safe: forget (removed block)"
echo 'not json' >"$tmp/bad.json"
expect_safe "$tmp/bad.json" false "plan-is-safe: unreadable plan"

echo
if ((failures > 0)); then
  echo "$failures test(s) failed."
  exit 1
fi
echo "All render-plan tests passed."
