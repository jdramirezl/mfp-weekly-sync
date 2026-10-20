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

# 1. uv, standalone -------------------------------------------------------------
# Not Homebrew: on Intel Macs Homebrew has no bottles for uv's toolchain and
# built LLVM, Rust and Python from source for hours before stalling. The
# official installer is pinned to UV_VERSION and downloaded to a file first
# (no curl | sh). It installs into ~/.local/bin, next to mfp-mcp.
export HOMEBREW_NO_AUTO_UPDATE=1
UV_BIN_DIR="$MFP_HOME/.local/bin"
if have uv; then
  UV="$(command -v uv)"
  log "uv already installed ($UV)"
elif [ -x "$UV_BIN_DIR/uv" ]; then
  UV="$UV_BIN_DIR/uv"
  log "uv already installed ($UV)"
else
  UV="$UV_BIN_DIR/uv"
  uv_url="https://astral.sh/uv/${UV_VERSION}/install.sh"
  if [ "$DRY_RUN" = 1 ]; then
    uv_tmp="${TMPDIR:-/tmp}/uv-install.XXXXXX"
  else
    uv_tmp="$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")"
    trap 'rm -f "$uv_tmp"' EXIT
  fi
  run curl -fsSL --proto '=https' --tlsv1.2 -o "$uv_tmp" "$uv_url"
  run env UV_INSTALL_DIR="$UV_BIN_DIR" sh "$uv_tmp"
  if [ "$DRY_RUN" != 1 ] && [ ! -x "$UV" ]; then
    die "uv installer finished but $UV is missing"
  fi
fi

# Homebrew is only needed for the optional tunnel.
if [ "$WITH_TUNNEL" = 1 ]; then
  if have tunnel-client; then
    log "tunnel-client already installed"
  elif ! have brew; then
    die "--with-tunnel needs Homebrew (https://brew.sh) for openai/tools/tunnel-client."
  else
    run env HOMEBREW_NO_AUTO_UPDATE=1 brew install openai/tools/tunnel-client
  fi
fi

# 2. Python 3.13 + Mason, pinned, with the autorefresh extra ---------------------
# Always pin the tool's Python: mfp-mcp 0.4.1 pins a pydantic-core that does
# not build on Python 3.15, which uv would otherwise pick when it is newest.
run "$UV" python install "$MASON_PYTHON"

TOOL_DIR=""
[ -x "$UV" ] && TOOL_DIR="$("$UV" tool dir 2>/dev/null || true)"
[ -n "$TOOL_DIR" ] || TOOL_DIR="$MFP_HOME/.local/share/uv/tools"
TOOL_PY="$TOOL_DIR/mfp-mcp/bin/python"
# uv reports the same "mfp-mcp v0.4.1" for the PyPI and the git build, so the
# exact spec that was installed is recorded next to the tool.
SPEC_MARKER="$TOOL_DIR/mfp-mcp/.mfp-mason-spec"

mason_installed() {
  [ -x "$UV" ] || return 1
  "$UV" tool list 2>/dev/null | grep -Eq "^mfp-mcp v${MFP_MCP_VERSION}( |\$)" || return 1
  [ -x "$TOOL_PY" ] || return 1
  [ "$(cat "$SPEC_MARKER" 2>/dev/null)" = "$MFP_MCP_SPEC" ] || return 1
  # Built on the pinned Python (an earlier install may have used 3.15).
  "$TOOL_PY" -c "import sys; raise SystemExit(sys.version_info[:2] != tuple(int(x) for x in '$MASON_PYTHON'.split('.')))" >/dev/null 2>&1 || return 1
  # The autorefresh extra brings Playwright into the tool's own environment.
  "$TOOL_PY" -c 'import playwright' >/dev/null 2>&1
}

if mason_installed; then
  log "mfp-mcp ${MFP_MCP_VERSION} @ ${MFP_MCP_GIT_REF:-pypi} (autorefresh, Python ${MASON_PYTHON}) already installed"
else
  case "$MFP_MCP_SPEC" in
    *git+*)
      if ! git --version >/dev/null 2>&1; then
        die "git is required to install the pinned Mason commit; run: xcode-select --install"
      fi
      ;;
  esac
  run "$UV" tool install --force --python "$MASON_PYTHON" "$MFP_MCP_SPEC"
  if [ "$DRY_RUN" != 1 ]; then
    mkdir -p "$(dirname "$SPEC_MARKER")"
    printf '%s\n' "$MFP_MCP_SPEC" >"$SPEC_MARKER"
  fi
fi

# 3. Headless Chromium for session auto-refresh. Use the tool's own Python so
#    the Playwright version always matches the one Mason imports. Playwright
#    skips the download when the browser is already present.
run "$TOOL_PY" -m playwright install chromium

# 3b. Login browser venv: patchright, separate from Mason's tool env -----------
# `mfpctl login` drives the installed Google Chrome through patchright (stock
# Playwright Chromium fails Cloudflare Turnstile even when a human clicks).
# channel="chrome" uses the installed Chrome, so no `patchright install`.
LOGIN_PY="$LOGIN_VENV/bin/python"
if login_venv_ok "$LOGIN_PY"; then
  log "patchright ${PATCHRIGHT_VERSION} login venv already installed"
else
  run mkdir -p "$MFP_STATE_DIR"
  [ -x "$LOGIN_PY" ] || run "$UV" venv --python "$MASON_PYTHON" "$LOGIN_VENV"
  run "$UV" pip install --python "$LOGIN_PY" "patchright==$PATCHRIGHT_VERSION"
fi
chrome_installed || warn "Google Chrome not found in /Applications; it is needed for mfpctl login. Install Google Chrome."

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
  1. Bot login (recommended): add MFP_LOGIN_EMAIL to mason.env, then
       security add-generic-password -s mfp-login -a <email> -w   # prompts
       bin/mfpctl login         # at the screen
     Fallback: pbpaste | bin/mfp-auth  # after copying the full Cookie request header
  2. mfp-mcp auth --check    # should say "Status: valid" and "Auto-refresh: ready"
  3. bin/mfpctl install      # start the launchd services
  4. cp "$MFP_CONFIG_DIR/alert.env.example" "$MFP_CONFIG_DIR/alert.env" && chmod 600 "$MFP_CONFIG_DIR/alert.env"
     then fill in the Telegram values and run: bin/mfpctl health
EOF
