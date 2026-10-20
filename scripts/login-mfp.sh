#!/bin/bash
# Log the bot into MyFitnessPal with its OWN email + password (from the macOS
# Keychain). Run by `mfpctl login` at the Mac's screen, or automatically by
# the health check when the session is dead.
#
# Why its own login: a session copied from your browser shares one NextAuth
# lineage with it, and whichever side refreshes first can invalidate the other.
#
# How: mfp_login.py runs in a small patchright venv and drives the INSTALLED
# Google Chrome (stock Playwright Chromium is flagged by Cloudflare Turnstile
# even when a human clicks), in its own login profile. It writes the harvested
# cookies to a 0600 temp file; save_session.py (Mason's Python) then saves them
# with Mason's auth.save_cookies and seeds Mason's own browser-profile.
#
# --manual: for when Turnstile still fails. Opens real Chrome on the login
# profile with no automation at all; you log in by hand and quit Chrome (⌘Q);
# then the cookies are read from that profile. Needs someone at the screen.
#
# Needs (automatic mode): MFP_LOGIN_EMAIL in ~/.config/mfp-mason/mason.env and
#   security add-generic-password -s mfp-login -a <email> -w   # prompts
# The password is read only inside mfp_login.py; this script never touches it.
#
# Usage: login-mfp.sh [--manual] [--dry-run] [--no-health] [--timeout SECONDS]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

RUN_HEALTH=1
MANUAL=0
TIMEOUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --manual) MANUAL=1 ;;
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
if [ -z "$TIMEOUT" ]; then
  if [ "$MANUAL" = 1 ]; then TIMEOUT=600; else TIMEOUT=180; fi
fi
case "$TIMEOUT" in *[!0-9]*) die "--timeout takes a number of seconds" ;; esac

EXIT_ENV=8
mfp_init_paths
load_mason_env
EMAIL="${MFP_LOGIN_EMAIL:-}"
LOGIN_SCRIPT="$SCRIPT_DIR/mfp_login.py"
SAVE_SCRIPT="$SCRIPT_DIR/save_session.py"
LOG_FILE="$MFP_LOG_DIR/login.log"

env_missing() {
  printf '❌ Login failed (exit %s: login environment missing (%s); run scripts/install-mac.sh)\n' "$EXIT_ENV" "$1" >&2
  exit "$EXIT_ENV"
}

if [ "$MANUAL" = 1 ]; then
  if [ ! -t 0 ] && [ "${MFP_LOGIN_ALLOW_MANUAL:-0}" != 1 ]; then
    die "--manual needs you at the Mac's screen (run it from a Terminal there); it is never used unattended."
  fi
else
  [ -n "$EMAIL" ] || die "MFP_LOGIN_EMAIL is not set. Add MFP_LOGIN_EMAIL=<email> to $MFP_CONFIG_DIR/mason.env (see docs/mac-runbook.md step 4)."
  keychain_has_login "$EMAIL" ||
    die "no Keychain password for $LOGIN_KEYCHAIN_SERVICE. Add it (prompts for the password): security add-generic-password -s $LOGIN_KEYCHAIN_SERVICE -a '$EMAIL' -w"
fi
if [ ! -f "$LOGIN_SCRIPT" ] || [ ! -f "$SAVE_SCRIPT" ]; then
  die "missing $LOGIN_SCRIPT or $SAVE_SCRIPT; run bin/mfpctl install"
fi
LOGIN_PY="$(resolve_login_python)"
TOOL_PY="$(resolve_tool_python)"
BIN="$(resolve_mfp_mcp_bin)"

before="$(session_token_hash)"
if [ "$DRY_RUN" = 1 ]; then
  printf 'dry run: session token hash now %s\n' "$before"
  launchctl print "gui/$(id -u)/$LABEL_MASON" >/dev/null 2>&1 && printf '+ launchctl bootout gui/%s/%s\n' "$(id -u)" "$LABEL_MASON"
  if [ "$MANUAL" = 1 ]; then
    printf '+ open -na "Google Chrome" --args --user-data-dir=%q --no-first-run %s\n' "$LOGIN_PROFILE" "$LOGIN_URL"
    printf '+ wait for that Chrome to quit (timeout %ss)\n' "$TIMEOUT"
    printf '+ %q %q --manual   (read cookies from the login profile)\n' "$LOGIN_PY" "$LOGIN_SCRIPT"
  else
    printf '+ %q %q   (installed Chrome via patchright, email from mason.env, password from Keychain %s)\n' "$LOGIN_PY" "$LOGIN_SCRIPT" "$LOGIN_KEYCHAIN_SERVICE"
  fi
  printf '+ %q %q   (Mason: save_cookies + seed_profile)\n' "$TOOL_PY" "$SAVE_SCRIPT"
  printf '+ launchctl bootstrap (if it was loaded), then %q auth --check\n' "$BIN"
  [ "$RUN_HEALTH" = 1 ] && printf '+ health-check.sh --wait 120 --no-heal\n'
  exit 0
fi

mkdir -p "$MFP_LOG_DIR" "$MFP_STATE_DIR"
# Record the attempt first: the re-login rate limit counts failures too. The
# exit code is added once known (exit 6 = Turnstile/timeout retries sooner).
attempt_at="$(date +%s)"
printf '%s\n' "$attempt_at" >"$MFP_STATE_DIR/relogin.last"
record_exit() { printf '%s %s\n' "$attempt_at" "$1" >"$MFP_STATE_DIR/relogin.last"; }

# The login environment: patchright venv + installed Chrome + Mason's Python.
"$LOGIN_PY" -c 'import patchright' >/dev/null 2>&1 || { record_exit "$EXIT_ENV"; env_missing "patchright venv at $LOGIN_VENV"; }
chrome_installed || { record_exit "$EXIT_ENV"; env_missing "Google Chrome"; }
[ -x "$TOOL_PY" ] || { record_exit "$EXIT_ENV"; env_missing "Mason's tool Python at $TOOL_PY"; }

mkdir -p "$LOGIN_PROFILE"
chmod 700 "$LOGIN_PROFILE"
cookie_tmp="$(mktemp "$MFP_STATE_DIR/login-cookies.XXXXXX")"
chmod 600 "$cookie_tmp"
# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  rm -f "$cookie_tmp"
  mason_restore
}
trap cleanup EXIT
mason_stop_for_profile

printf '%s login start (%s, timeout %ss)\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
  "$([ "$MANUAL" = 1 ] && echo manual || echo automatic)" "$TIMEOUT" >>"$LOG_FILE"

login_args=()
if [ "$MANUAL" = 1 ]; then
  login_args=(--manual)
  cat <<'EOF' | tee -a "$LOG_FILE"
Se abrirá una ventana de Chrome. Inicia sesión en MyFitnessPal ahí (correo +
contraseña; resuelve la casilla de Cloudflare) y luego CIERRA Chrome (⌘Q).
A Chrome window will open. Log in to MyFitnessPal there (email + password;
solve the Cloudflare box), then QUIT that Chrome window (⌘Q).
EOF
  open -na "Google Chrome" --args --user-data-dir="$LOGIN_PROFILE" --no-first-run "$LOGIN_URL"
  sleep 3
  waited=0
  while pgrep -f -- "--user-data-dir=$LOGIN_PROFILE" >/dev/null 2>&1; do
    if [ "$waited" -ge "$TIMEOUT" ]; then
      printf '❌ Login failed (exit 6: Chrome was not closed within %ss; quit it with ⌘Q and re-run)\n' "$TIMEOUT" | tee -a "$LOG_FILE"
      record_exit 6
      exit 6
    fi
    if [ "$waited" -gt 0 ] && [ $((waited % 60)) -eq 0 ]; then
      printf 'Esperando a que cierres Chrome (⌘Q)… / Waiting for you to quit Chrome (⌘Q)… %ss\n' "$waited"
    fi
    sleep 2
    waited=$((waited + 2))
  done
fi

set +e
MFP_LOGIN_EMAIL="$EMAIL" MFP_LOGIN_KEYCHAIN_SERVICE="$LOGIN_KEYCHAIN_SERVICE" \
  MFP_LOGIN_PROFILE="$LOGIN_PROFILE" MFP_COOKIE_OUT="$cookie_tmp" \
  MFP_LOG_DIR="$MFP_LOG_DIR" MFP_LOGIN_TIMEOUT="$TIMEOUT" \
  "$LOGIN_PY" "$LOGIN_SCRIPT" ${login_args[@]+"${login_args[@]}"} 2>&1 | tee -a "$LOG_FILE"
rc="${PIPESTATUS[0]}"
if [ "$rc" -eq 0 ]; then
  MFP_COOKIE_IN="$cookie_tmp" MFP_MCP_DATA_DIR="$MFP_MCP_DATA_DIR" MFP_USERNAME="${MFP_USERNAME:-}" \
    "$TOOL_PY" "$SAVE_SCRIPT" 2>&1 | tee -a "$LOG_FILE"
  [ "${PIPESTATUS[0]}" -eq 0 ] || rc=7
fi
set -e
rm -f "$cookie_tmp"
record_exit "$rc"

mason_restore

after="$(session_token_hash)"
meaning() {
  case "$1" in
    4) echo "no Keychain password" ;;
    5) echo "MFP rejected the email/password (fields verified before submit): check the Keychain password with bin/mfpctl login --manual or a Guest-window login" ;;
    6) echo "timed out: Turnstile/captcha not cleared; try bin/mfpctl login --manual at the screen" ;;
    9) echo "MFP rejected the login after a cookie-consent banner / field problem (inputs may have been lost); re-run bin/mfpctl login at the screen" ;;
    8) echo "login environment missing (patchright venv or Chrome); run scripts/install-mac.sh" ;;
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
