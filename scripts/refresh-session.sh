#!/bin/bash
# Weekly proactive MyFitnessPal session refresh (com.jdrami.mfp-refresh, Sunday
# 04:30), or on demand with `mfpctl refresh`.
#
# Mason only refreshes reactively, when a tool call hits an auth error. A
# session that sits idle longer than MFP is willing to extend it can expire
# with no refresh ever attempted. This job calls Mason's own public
# refresh.refresh_session() from the tool's Python on a schedule. It opens the
# seeded headless Chromium profile, visits myfitnesspal.com, and saves the
# session cookie MFP hands back. It never performs an SSO/Google/Apple login.
# If MFP has ended or revoked the session, nothing can be harvested; the
# health check that follows then alerts, and the fix is
# `pbpaste | bin/mfp-auth`.
#
# Mason is stopped for the duration so two processes never share the Chromium
# profile or keep a stale cached client, and it is ALWAYS restarted afterwards.
# Only an 8-character hash of the token is logged, never the token itself.
#
# Usage: refresh-session.sh [--dry-run] [--no-health]
# Exit status: non-zero only when the refresh itself failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

RUN_HEALTH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-health) RUN_HEALTH=0 ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

mfp_init_paths
load_mason_env
DOMAIN="gui/$(id -u)"
MASON_PLIST="$LAUNCH_AGENTS_DIR/$LABEL_MASON.plist"
PROFILE="$MFP_MCP_DATA_DIR/browser-profile"
LOG_FILE="$MFP_LOG_DIR/refresh.log"

record() {
  printf '%s\n' "$*"
  [ "$DRY_RUN" = 1 ] && return 0
  mkdir -p "$MFP_LOG_DIR"
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG_FILE"
}

[ -d "$PROFILE" ] || die "no seeded browser profile at $PROFILE. Auto-refresh needs one: run pbpaste | bin/mfp-auth"
TOOL_PY="$(resolve_tool_python)"
[ -x "$TOOL_PY" ] || die "Mason's tool Python not found at $TOOL_PY; re-run scripts/install-mac.sh"

before="$(session_token_hash)"

if [ "$DRY_RUN" = 1 ]; then
  printf 'dry run: session token hash now %s\n' "$before"
  launchctl print "$DOMAIN/$LABEL_MASON" >/dev/null 2>&1 && printf '+ launchctl bootout %s/%s\n' "$DOMAIN" "$LABEL_MASON"
  printf '+ %q -c <refresh.refresh_session()>\n' "$TOOL_PY"
  printf '+ launchctl bootstrap %s %q (if it was loaded)\n' "$DOMAIN" "$MASON_PLIST"
  [ "$RUN_HEALTH" = 1 ] && printf '+ health-check.sh --wait 120 --no-heal\n'
  exit 0
fi

mkdir -p "$MFP_LOG_DIR"

trap mason_restore EXIT
mason_stop_for_profile

rc=0
MFP_MCP_DATA_DIR="$MFP_MCP_DATA_DIR" MFP_USERNAME="${MFP_USERNAME:-}" "$TOOL_PY" -c '
import sys
from myfitnesspal_mcp import refresh
if not (refresh.available() and refresh.profile_seeded()):
    print("auto-refresh unavailable (Playwright missing or profile not seeded)", file=sys.stderr)
    sys.exit(3)
refresh.refresh_session()
' >>"$MFP_LOG_DIR/refresh.err.log" 2>&1 || rc=$?

after="$(session_token_hash)"
if [ "$rc" -ne 0 ]; then
  record "refresh FAILED (exit $rc); token $before -> $after. See $MFP_LOG_DIR/refresh.err.log"
elif [ "$after" = none ]; then
  rc=1
  record "refresh FAILED: no session token saved. Run: pbpaste | bin/mfp-auth"
elif [ "$after" != "$before" ]; then
  record "refresh ok; rotated: yes ($before -> $after)"
else
  record "refresh ok; rotated: no (token unchanged, $after)"
fi

mason_restore

# Confirm the session still works; alerts on Telegram only if state changed.
# --no-heal: this script is itself part of the healing path.
if [ "$RUN_HEALTH" = 1 ]; then
  /bin/bash "$SCRIPT_DIR/health-check.sh" --wait 120 --no-heal || true
fi
exit "$rc"
