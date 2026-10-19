#!/bin/bash
# Install Mason's MyFitnessPal MCP (mfp-mcp) and its dependencies on macOS.
# Idempotent: safe to re-run. Never runs `mfp-mcp auth`; that step needs you.
#
# Usage: scripts/install-mac.sh [--dry-run] [--with-tunnel] [--username NAME]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

WITH_TUNNEL=0
USERNAME_ARG=""
usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run] [--with-tunnel] [--username NAME]

  --dry-run      Print every command instead of running it; change nothing.
  --with-tunnel  Also install OpenAI's tunnel-client (for optional ChatGPT access).
  --username N   Your MyFitnessPal username (not email); saved to mason.env.
                 Required for SSO/email logins.
EOF
}
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --with-tunnel) WITH_TUNNEL=1 ;;
    --username)
      [ $# -ge 2 ] || die "--username needs a value"
      USERNAME_ARG="$2"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option: $1"
      ;;
  esac
  shift
done

require_darwin
mfp_init_paths

have() { command -v "$1" >/dev/null 2>&1; }

# 1. Homebrew packages ---------------------------------------------------------
if ! have brew; then
  die "Homebrew is required. Install it from https://brew.sh, then re-run."
fi
if have uv; then
  log "uv already installed"
else
  run brew install uv
fi
if [ "$WITH_TUNNEL" = 1 ]; then
  if have tunnel-client; then
    log "tunnel-client already installed"
  else
    run brew install openai/tools/tunnel-client
  fi
fi

# 2. Mason, pinned, with the autorefresh extra ---------------------------------
uv_tool_dir() {
  if have uv; then uv tool dir 2>/dev/null && return 0; fi
  printf '%s\n' "$MFP_HOME/.local/share/uv/tools"
}
TOOL_PY="$(uv_tool_dir)/mfp-mcp/bin/python"

mason_installed() {
  have uv || return 1
  uv tool list 2>/dev/null | grep -Eq "^mfp-mcp v${MFP_MCP_VERSION}( |\$)" || return 1
  # The autorefresh extra brings Playwright into the tool's own environment.
  [ -x "$TOOL_PY" ] && "$TOOL_PY" -c 'import playwright' >/dev/null 2>&1
}

if mason_installed; then
  log "mfp-mcp ${MFP_MCP_VERSION} (autorefresh) already installed"
else
  run uv tool install --force "$MFP_MCP_SPEC"
fi

# 3. Headless Chromium for session auto-refresh. Use the tool's own Python so
#    the Playwright version always matches the one Mason imports. Playwright
#    skips the download when the browser is already present.
run "$TOOL_PY" -m playwright install chromium

# 4. Private folders -------------------------------------------------------------
for d in "$MFP_MCP_DATA_DIR" "$MFP_STATE_DIR" "$MFP_LOG_DIR" "$MFP_CONFIG_DIR"; do
  run mkdir -p "$d"
  run chmod 700 "$d"
done

# 5. Config examples (never overwrite real env files) -----------------------------
examples="alert.env.example mason.env.example"
[ "$WITH_TUNNEL" = 1 ] && examples="$examples tunnel.env.example"
for f in $examples; do
  run cp "$REPO_ROOT/config/$f" "$MFP_CONFIG_DIR/$f"
  run chmod 600 "$MFP_CONFIG_DIR/$f"
done

# 6. MyFitnessPal username ------------------------------------------------------
# MFP's profile endpoint 500s for SSO/email logins, and Mason then needs
# MFP_USERNAME. Store it in mason.env (0600), which the service loads at start.
MASON_ENV="$MFP_CONFIG_DIR/mason.env"
existing=""
if [ -f "$MASON_ENV" ]; then
  existing="$(load_env_file "$MASON_ENV" MFP_USERNAME; printf '%s' "${MFP_USERNAME:-}")"
fi
if [ -n "$existing" ]; then
  log "MFP_USERNAME already set in $MASON_ENV"
else
  name="$USERNAME_ARG"
  if [ -z "$name" ] && { [ -t 0 ] || [ "${MFP_INSTALL_ASSUME_TTY:-0}" = 1 ]; }; then
    printf 'MyFitnessPal username (not email), Enter to skip: ' >&2
    IFS= read -r name || true
  fi
  if [ -n "$name" ]; then
    case "$name" in *@* | *[!A-Za-z0-9_.-]*) die "'$name' doesn't look like an MFP username (not your email)" ;; esac
    if [ "$DRY_RUN" = 1 ]; then
      printf '+ append MFP_USERNAME=%s to %q (mode 600)\n' "$name" "$MASON_ENV"
    else
      mkdir -p "$MFP_CONFIG_DIR"
      [ -f "$MASON_ENV" ] || (umask 077 && : >"$MASON_ENV")
      # Keep existing lines; make sure the file ends with a newline first.
      if [ -s "$MASON_ENV" ] && [ -n "$(tail -c 1 "$MASON_ENV")" ]; then printf '\n' >>"$MASON_ENV"; fi
      printf 'MFP_USERNAME=%s\n' "$name" >>"$MASON_ENV"
      chmod 600 "$MASON_ENV"
      log "saved MFP_USERNAME to $MASON_ENV"
    fi
  elif ! mason_saved_username >/dev/null 2>&1; then
    warn "$USERNAME_HINT"
  fi
fi

cat <<EOF

$( [ "$DRY_RUN" = 1 ] && echo "Dry run: nothing was changed." || echo "Install step done.") Next, in a normal Terminal window:
  0. If "mfp-mcp: command not found": uv tool update-shell, then open a new Terminal
  1. pbpaste | bin/mfp-auth  # after copying the __Secure-next-auth.session-token value
  2. mfp-mcp auth --check    # should say "Status: valid" and "Auto-refresh: ready"
  3. bin/mfpctl install      # start the launchd services
  4. cp "$MFP_CONFIG_DIR/alert.env.example" "$MFP_CONFIG_DIR/alert.env" && chmod 600 "$MFP_CONFIG_DIR/alert.env"
     then fill in the Telegram values and run: bin/mfpctl health
EOF
