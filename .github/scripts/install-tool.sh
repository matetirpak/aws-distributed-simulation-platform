#!/usr/bin/env bash
# Install pinned, checksum-verified tool binaries listed in .github/tools.lock.
#
#   install-tool.sh NAME [NAME...]
#
# Binaries land in $TOOLS_DIR/bin (default: $RUNNER_TEMP/tools or ./.tools) and,
# inside GitHub Actions, that directory is added to GITHUB_PATH. The TFLint aws
# ruleset is placed in TFLINT_PLUGIN_DIR so `tflint --init` never downloads.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
lock_file="$repo_root/.github/tools.lock"
tools_dir=${TOOLS_DIR:-${RUNNER_TEMP:-$repo_root/.tools}/tools}
bin_dir="$tools_dir/bin"
dl_dir="$tools_dir/dl"
plugin_dir=${TFLINT_PLUGIN_DIR:-$tools_dir/tflint-plugins}
mkdir -p "$bin_dir" "$dl_dir" "$plugin_dir"

[[ $# -gt 0 ]] || { echo "usage: $0 NAME [NAME...]" >&2; exit 2; }

for name in "$@"; do
  line=$(awk -v n="$name" '!/^[[:space:]]*#/ && $1 == n' "$lock_file")
  [[ -n "$line" ]] || { echo "::error::$name is not listed in .github/tools.lock" >&2; exit 1; }
  read -r _ version member sha256 url <<<"$line"

  if [[ ! "$url" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/ ]]; then
    echo "::error::$name: only GitHub release downloads are allowed, got $url" >&2
    exit 1
  fi
  if [[ ! "$sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "::error::$name: malformed sha256 in tools.lock" >&2
    exit 1
  fi

  archive="$dl_dir/${url##*/}"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 3 --output "$archive" "$url"

  if ! echo "$sha256  $archive" | sha256sum --check --strict --quiet; then
    echo "::error::$name $version: SHA-256 mismatch for $url. Refusing to install." >&2
    exit 1
  fi

  unpack_dir=$(mktemp -d "$dl_dir/$name.XXXXXX")
  case "$archive" in
    *.zip) unzip -q -o "$archive" "$member" -d "$unpack_dir" ;;
    *.tar.gz) tar -xzf "$archive" -C "$unpack_dir" "$member" ;;
    *) echo "::error::$name: unsupported archive type $archive" >&2; exit 1 ;;
  esac

  if [[ "$name" == tflint-ruleset-* ]]; then
    dest="$plugin_dir/github.com/terraform-linters/$name/$version"
    mkdir -p "$dest"
    install -m 0755 "$unpack_dir/$member" "$dest/$member"
  else
    install -m 0755 "$unpack_dir/$member" "$bin_dir/${member##*/}"
  fi
  echo "Installed $name $version (sha256 $sha256)"
done

if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$bin_dir" >>"$GITHUB_PATH"
  echo "TFLINT_PLUGIN_DIR=$plugin_dir" >>"$GITHUB_ENV"
else
  echo "Add to your shell: export PATH=\"$bin_dir:\$PATH\" TFLINT_PLUGIN_DIR=\"$plugin_dir\""
fi
