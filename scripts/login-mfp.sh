#!/bin/bash
# Log the bot into MyFitnessPal with its OWN email + password (from the macOS
# Keychain), in Mason's Chromium profile. Run by `mfpctl login` at the Mac's
# screen, or automatically by the health check when the session is dead.
#
# Why: a session copied from your browser shares one NextAuth lineage with it,
# and whichever side refreshes first can invalidate the other. A login of the
# bot's own is independent, so you can keep using MFP on your phone/browser.
#
# Needs MFP_LOGIN_EMAIL in ~/.config/mfp-mason/mason.env and a Keychain item:
#   security add-generic-password -s mfp-login -a <email> -w   # prompts
# The password is read only inside mfp_login.py; this script never touches it.
#
# Usage: login-mfp.sh [--dry-run] [--no-health] [--timeout SECONDS]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

RUN_HEALTH=1
TIMEOUT=180
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-health) RUN_HEALTH=0 ;;
    --timeout)
      TIMEOUT="${2:-}"
      shift
      ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
case "$TIMEOUT" in '' | *[!0-9]*) die "--timeout takes a number of seconds" ;; esac

mfp_init_paths
load_mason_env
EMAIL="${MFP_LOGIN_EMAIL:-}"
LOGIN_PY="$SCRIPT_DIR/mfp_login.py"
LOG_FILE="$MFP_LOG_DIR/login.log"

[ -n "$EMAIL" ] || die "MFP_LOGIN_EMAIL is not set. Add MFP_LOGIN_EMAIL=<email> to $MFP_CONFIG_DIR/mason.env (see docs/mac-runbook.md step 4)."
keychain_has_login "$EMAIL" ||
  die "no Keychain password for $LOGIN_KEYCHAIN_SERVICE. Add it (prompts for the password): security add-generic-password -s $LOGIN_KEYCHAIN_SERVICE -a '$EMAIL' -w"
[ -f "$LOGIN_PY" ] || die "missing $LOGIN_PY; run bin/mfpctl install"
TOOL_PY="$(resolve_tool_python)"
[ -x "$TOOL_PY" ] || die "Mason's tool Python not found at $TOOL_PY; re-run scripts/install-mac.sh"
BIN="$(resolve_mfp_mcp_bin)"

before="$(session_token_hash)"
if [ "$DRY_RUN" = 1 ]; then
  printf 'dry run: session token hash now %s\n' "$before"
  launchctl print "gui/$(id -u)/$LABEL_MASON" >/dev/null 2>&1 && printf '+ launchctl bootout gui/%s/%s\n' "$(id -u)" "$LABEL_MASON"
  printf '+ %q %q   (headed Chromium, email from mason.env, password from Keychain %s)\n' "$TOOL_PY" "$LOGIN_PY" "$LOGIN_KEYCHAIN_SERVICE"
  printf '+ launchctl bootstrap (if it was loaded), then %q auth --check\n' "$BIN"
  [ "$RUN_HEALTH" = 1 ] && printf '+ health-check.sh --wait 120 --no-heal\n'
  exit 0
fi

mkdir -p "$MFP_LOG_DIR" "$MFP_STATE_DIR"
# Record the attempt first: the 6-hour re-login rate limit counts failures too.
date +%s >"$MFP_STATE_DIR/relogin.last"

trap mason_restore EXIT
mason_stop_for_profile

printf '%s login start (timeout %ss)\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$TIMEOUT" >>"$LOG_FILE"
set +e
MFP_LOGIN_EMAIL="$EMAIL" MFP_LOGIN_KEYCHAIN_SERVICE="$LOGIN_KEYCHAIN_SERVICE" \
  MFP_MCP_DATA_DIR="$MFP_MCP_DATA_DIR" MFP_USERNAME="${MFP_USERNAME:-}" \
  MFP_LOG_DIR="$MFP_LOG_DIR" MFP_LOGIN_TIMEOUT="$TIMEOUT" \
  "$TOOL_PY" "$LOGIN_PY" 2>&1 | tee -a "$LOG_FILE"
rc="${PIPESTATUS[0]}"
set -e

mason_restore

after="$(session_token_hash)"
meaning() {
  case "$1" in
    4) echo "no Keychain password" ;;
    5) echo "MFP rejected the email/password" ;;
    6) echo "timed out waiting for a session (captcha/2FA not finished?)" ;;
    *) echo "unexpected error, see $LOG_FILE" ;;
  esac
}

result=0
if [ "$rc" -ne 0 ]; then
  printf '❌ Login failed (exit %s: %s). See %s\n' "$rc" "$(meaning "$rc")" "$LOG_FILE" | tee -a "$LOG_FILE"
  result="$rc"
else
  check_rc=0
  check="$(MFP_USERNAME="${MFP_USERNAME:-}" MFP_MCP_DATA_DIR="$MFP_MCP_DATA_DIR" "$BIN" auth --check 2>&1)" || check_rc=$?
  status_line="$(printf '%s\n' "$check" | sed -n 's/^Status:[[:space:]]*//p' | head -n 1)"
  if [ "$check_rc" -eq 0 ]; then
    user="$(printf '%s' "$status_line" | sed -n 's/.*connected as //p')"
    printf '✅ Logged in as %s (token %s -> %s)\n' "${user:-?}" "$before" "$after" | tee -a "$LOG_FILE"
  else
    printf '❌ Login saved a session but auth --check failed: %s\n' "${status_line:-exit $check_rc}" | tee -a "$LOG_FILE"
    result=1
  fi
fi

if [ "$RUN_HEALTH" = 1 ]; then
  /bin/bash "$SCRIPT_DIR/health-check.sh" --wait 120 --no-heal || true
fi
exit "$result"
