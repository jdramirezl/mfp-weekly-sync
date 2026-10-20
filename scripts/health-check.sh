#!/bin/bash
# Daily health check for the Mason service. Sends a Telegram message ONLY when
# the overall state changes (ok -> failing, failing -> different failure,
# failing -> ok). Run by the com.jdrami.mfp-health LaunchAgent every 6 h (03,09,15,21) and at
# load, or on demand with `mfpctl health`.
#
# Checks:
#   mcp     Mason answers an MCP initialize POST on 127.0.0.1:$MFP_PORT/mcp.
#   auth    `mfp-mcp auth --check` exits 0. Per Mason 0.4.1 auth.run_check():
#             exit 0  "Status:       valid, connected as <user>"
#             exit 1  "Session:      none saved"         (never authenticated)
#             exit 1  "Status:       rejected (<error>)"  (expired / blocked)
#           It is read-only (never prompts or rewrites the cookie) but does make
#           one live request to MyFitnessPal, which is why this runs every 6 h, not
#           in a loop.
#   tunnel  Only if the tunnel LaunchAgent is installed: tunnel-client's
#           http://$TUNNEL_HEALTH_ADDR/readyz returns 200.
#
# Usage: health-check.sh [--dry-run] [--simulate-fail mcp|auth|tunnel] [--wait SECONDS]
#   --dry-run        Run the checks but only print the alert that would be sent;
#                    never sends, never writes state.
#   --simulate-fail  Force one check to fail (to preview the alert text).
#   --wait SECONDS   Retry the mcp check for up to SECONDS (boot grace period).
#
# Exit status: 0 when healthy, 1 when any check fails.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

SIMULATE=""
NO_HEAL=0
WAIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-heal) export NO_HEAL=1 ;;
    --simulate-fail)
      SIMULATE="${2:-}"
      shift
      ;;
    --wait)
      WAIT="${2:-0}"
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
case "$SIMULATE" in '' | mcp | auth | tunnel) ;; *) die "--simulate-fail takes mcp, auth or tunnel" ;; esac
case "$WAIT" in *[!0-9]*) die "--wait takes a number of seconds" ;; esac

mfp_init_paths
load_mason_env
STATE_FILE="$MFP_STATE_DIR/health.state"
ALERT_ENV="$MFP_CONFIG_DIR/alert.env"
TUNNEL_PLIST="$LAUNCH_AGENTS_DIR/$LABEL_TUNNEL.plist"

FAILED=""  # comma-separated failing check names
DETAILS="" # one line per failing check

fail() {
  FAILED="${FAILED:+$FAILED,}$1"
  DETAILS="${DETAILS}
- $1: $2 → $3"
}

hint_for() {
  case "$1" in
    mcp) printf '%s' "run \`mfpctl restart\`; logs in ~/Library/Logs/mfp-mason" ;;
    auth) printf '%s' "run \`mfp-mcp auth\` with a fresh cookie (auto-refresh may also recover on the next tool call)" ;;
    tunnel) printf '%s' "check ~/.config/mfp-mason/tunnel.env or run \`mfpctl tunnel disable\`" ;;
  esac
}

# --- checks -----------------------------------------------------------------
check_mcp() {
  local waited=0
  if [ "$SIMULATE" = mcp ]; then
    fail mcp "simulated failure" "$(hint_for mcp)"
    return
  fi
  while ! mcp_initialize >/dev/null; do
    if [ "$waited" -ge "$WAIT" ]; then
      fail mcp "no MCP response on $MFP_MCP_URL" "$(hint_for mcp)"
      return
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

find_mfp_mcp() {
  if [ -n "${MFP_MCP_BIN:-}" ] && [ -x "$MFP_MCP_BIN" ]; then
    printf '%s' "$MFP_MCP_BIN"
  elif command -v mfp-mcp >/dev/null 2>&1; then
    command -v mfp-mcp
  elif [ -x "$MFP_HOME/.local/bin/mfp-mcp" ]; then
    printf '%s' "$MFP_HOME/.local/bin/mfp-mcp"
  else
    return 1
  fi
}

check_auth() {
  local bin out rc detail
  if [ "$SIMULATE" = auth ]; then
    fail auth "simulated failure" "$(hint_for auth)"
    return
  fi
  if ! bin="$(find_mfp_mcp)"; then
    fail auth "mfp-mcp not found" "re-run scripts/install-mac.sh"
    return
  fi
  rc=0
  out="$("$bin" auth --check 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] && return
  detail="$(printf '%s\n' "$out" | grep -E '^(Status|Session):' | tail -n 1 |
    sed -e 's/[[:space:]][[:space:]]*/ /g' | cut -c1-200)"
  fail auth "${detail:-auth --check exited $rc}" "$(hint_for auth)"
}

check_tunnel() {
  [ -e "$TUNNEL_PLIST" ] || return 0
  if [ "$SIMULATE" = tunnel ]; then
    fail tunnel "simulated failure" "$(hint_for tunnel)"
    return
  fi
  curl -fsS --max-time 5 -o /dev/null "http://$TUNNEL_HEALTH_ADDR/readyz" 2>/dev/null ||
    fail tunnel "tunnel-client /readyz is not 200" "$(hint_for tunnel)"
}

check_mcp
check_auth
check_tunnel

if [ -n "$FAILED" ]; then STATE="fail:$FAILED"; else STATE="ok"; fi
PREV="$(cat "$STATE_FILE" 2>/dev/null || true)"
PREV_EFFECTIVE="${PREV:-ok}"

record() {
  [ "$DRY_RUN" = 1 ] && return 0
  mkdir -p "$MFP_LOG_DIR"
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$MFP_LOG_DIR/health.log"
}

write_state() {
  [ "$DRY_RUN" = 1 ] && return 0
  mkdir -p "$MFP_STATE_DIR"
  printf '%s\n' "$STATE" >"$STATE_FILE"
}

finish() {
  [ "$STATE" = ok ] && exit 0
  exit 1
}

printf 'health: %s%s\n' "$STATE" "$DETAILS"
record "state=$STATE prev=${PREV:-none}"

if [ "$STATE" = "$PREV_EFFECTIVE" ]; then
  [ -n "$PREV" ] || write_state
  printf 'unchanged since last run; no alert sent\n'
  finish
fi

HOST="$(hostname -s 2>/dev/null || echo mac)"
if [ "$STATE" = ok ]; then
  MESSAGE="🟢 MFP Mason recovered on $HOST (was ${PREV_EFFECTIVE#fail:} failing)"
else
  MESSAGE="🔴 MFP Mason unhealthy on $HOST${DETAILS}"
fi

if [ "$DRY_RUN" = 1 ]; then
  printf 'dry run: would send this Telegram message:\n%s\n' "$MESSAGE"
  finish
fi

# --- alert ------------------------------------------------------------------
if [ ! -f "$ALERT_ENV" ]; then
  warn "no $ALERT_ENV; alerts are not configured (state left unchanged so it alerts once configured)"
  record "alert skipped: no alert.env"
  finish
fi
mode="$(file_mode "$ALERT_ENV")"
case "$mode" in 600 | 400) ;; *) warn "$ALERT_ENV has mode $mode; run: chmod 600 \"$ALERT_ENV\"" ;; esac
TELEGRAM_BOT_TOKEN="" TELEGRAM_CHAT_ID=""
load_env_file "$ALERT_ENV" TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
if ! printf '%s' "$TELEGRAM_BOT_TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$' || [ -z "$TELEGRAM_CHAT_ID" ]; then
  warn "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing or malformed in $ALERT_ENV; alert not sent"
  record "alert skipped: bad alert.env"
  finish
fi

# The token goes to curl through a config on stdin, never argv, so it does not
# show up in `ps` output or in any log.
if printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" |
  curl -sS --fail --max-time 15 -o /dev/null -K - \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$MESSAGE" 2>/dev/null; then
  write_state
  record "alert sent"
  printf 'alert sent\n'
else
  warn "Telegram send failed; state left unchanged so the next run retries"
  record "alert send failed"
fi
finish
