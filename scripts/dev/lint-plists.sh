#!/bin/bash
# make lint helper: render every launchd template with placeholder values and
# check it with plutil -lint. Writes only to a temp dir.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/mfp-lint.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

status=0
for tpl in "$REPO_ROOT"/launchd/*.plist.in; do
  out="$tmp/$(basename "${tpl%.in}")"
  sed -e 's/@EXTRA_ENV@//g' \
    -e 's/@MFP_PORT@/8484/g' \
    -e 's/@[A-Z][A-Z0-9_]*@/\/tmp\/placeholder/g' "$tpl" >"$out"
  plutil -lint "$out" || status=1
done
exit "$status"
