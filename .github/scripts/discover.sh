#!/usr/bin/env bash
# Discover environments and Terraform roots from the files committed in git.
#   envs:  every infra/envs/<env>/ci.json
#   roots: every directory with a committed .terraform.lock.hcl (envs, bootstrap, tested modules)
# Writes envs=<json list> and roots=<json list> to $GITHUB_OUTPUT (or stdout).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

envs=$(git ls-files 'infra/envs/*/ci.json' | cut -d/ -f3 | sort -u)
roots=$(git ls-files 'infra/**/.terraform.lock.hcl' | xargs -r -n1 dirname | sort -u)
for e in $envs; do
  [[ "$e" =~ ^[a-z0-9-]+$ ]] || { echo "::error::invalid environment name: $e" >&2; exit 1; }
done
for r in $roots; do
  [[ "$r" =~ ^infra/[a-z0-9/_-]+$ ]] || { echo "::error::invalid root path: $r" >&2; exit 1; }
done

{
  echo "envs=$(jq -cnR '[inputs | select(length > 0)]' <<<"$envs")"
  echo "roots=$(jq -cnR '[inputs | select(length > 0)]' <<<"$roots")"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"
