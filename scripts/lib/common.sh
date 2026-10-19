#!/bin/bash
# shellcheck disable=SC2034  # constants are consumed by the scripts that source this
# Shared helpers for mfpctl and the scripts. Sourced, never executed.
# Must stay compatible with macOS's stock /bin/bash 3.2: no associative arrays,
# no mapfile, no ${var,,}.

# --- constants ------------------------------------------------------------
MFP_MCP_VERSION="0.4.1"
MFP_MCP_SPEC="mfp-mcp[autorefresh]==${MFP_MCP_VERSION}"
# Python for the Mason tool env. 0.4.1 pins a pydantic-core that does not build
# on Python 3.15, and Intel Homebrew has no bottles for newer Pythons.
MASON_PYTHON="3.13"
# Standalone uv installer, pinned (https://github.com/astral-sh/uv/releases).
UV_VERSION="0.13.1"
LABEL_REFRESH="com.jdrami.mfp-refresh"
LABEL_MASON="com.jdrami.mfp-mcp"
LABEL_HEALTH="com.jdrami.mfp-health"
LABEL_TUNNEL="com.jdrami.mfp-tunnel"
# Mason has no authentication. It must only ever listen on loopback.
REQUIRED_HOST="127.0.0.1"
SERVICE_PATH_BASE="/usr/bin:/bin:/usr/sbin:/sbin"

# --- paths (every one overridable for tests) ------------------------------
mfp_init_paths() {
  MFP_HOME="${MFP_HOME_OVERRIDE:-$HOME}"
  LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$MFP_HOME/Library/LaunchAgents}"
  # Mason's default platformdirs location on macOS. Its cookies.json lives in
  # the same folder (user_config_dir == user_data_dir on macOS), so pinning the
  # data dir to the default keeps `mfp-mcp auth` run from a terminal and the
  # launchd service pointed at the same session and browser profile.
  MFP_MCP_DATA_DIR="${MFP_MCP_DATA_DIR:-$MFP_HOME/Library/Application Support/myfitnesspal-mcp}"
  # Mason keeps cookies.json in platformdirs.user_config_dir, which ignores
  # MFP_MCP_DATA_DIR (same folder by default on macOS).
  MFP_MCP_CONFIG_DIR="${MFP_MCP_CONFIG_DIR:-$MFP_HOME/Library/Application Support/myfitnesspal-mcp}"
  MFP_STATE_DIR="${MFP_STATE_DIR:-$MFP_HOME/Library/Application Support/mfp-mason}"
  MFP_CONFIG_DIR="${MFP_CONFIG_DIR:-$MFP_HOME/.config/mfp-mason}"
  MFP_LOG_DIR="${MFP_LOG_DIR:-$MFP_HOME/Library/Logs/mfp-mason}"
  MFP_HOST="${MFP_HOST:-$REQUIRED_HOST}"
  MFP_PORT="${MFP_PORT:-8484}"
  TUNNEL_HEALTH_ADDR="${TUNNEL_HEALTH_ADDR:-127.0.0.1:8080}"
  MFP_MCP_URL="http://${REQUIRED_HOST}:${MFP_PORT}/mcp"
  export MFP_HOME LAUNCH_AGENTS_DIR MFP_MCP_DATA_DIR MFP_MCP_CONFIG_DIR MFP_STATE_DIR MFP_CONFIG_DIR \
    MFP_LOG_DIR MFP_HOST MFP_PORT TUNNEL_HEALTH_ADDR MFP_MCP_URL
}

# --- logging / execution ---------------------------------------------------
DRY_RUN="${DRY_RUN:-0}"

log() { printf '[mfp-mason] %s\n' "$*" >&2; }
warn() { printf '[mfp-mason] WARNING: %s\n' "$*" >&2; }
die() {
  printf '[mfp-mason] ERROR: %s\n' "$*" >&2
  exit 1
}

# run CMD ARGS... : execute, or print it (shell-quoted) when DRY_RUN=1.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    local out="+" a
    for a in "$@"; do out="$out $(printf '%q' "$a")"; done
    printf '%s\n' "$out"
  else
    "$@"
  fi
}

require_darwin() {
  local os
  os="$(uname -s)"
  [ "$os" = Darwin ] || die "macOS only (uname -s reported '$os')."
}

require_loopback_host() {
  [ "$MFP_HOST" = "$REQUIRED_HOST" ] ||
    die "Refusing MFP_HOST='$MFP_HOST': Mason has no auth and must bind $REQUIRED_HOST only."
}

file_mode() { stat -f %Lp "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

# --- binaries -------------------------------------------------------------------
# resolve_mfp_mcp_bin : absolute path to Mason's mfp-mcp entry point.
resolve_mfp_mcp_bin() {
  local d p
  if [ -n "${MFP_MCP_BIN:-}" ]; then
    case "$MFP_MCP_BIN" in /*) ;; *) die "MFP_MCP_BIN must be an absolute path" ;; esac
    printf '%s\n' "$MFP_MCP_BIN"
    return
  fi
  if have uv && d="$(uv tool dir --bin 2>/dev/null)" && [ -x "$d/mfp-mcp" ]; then
    printf '%s\n' "$d/mfp-mcp"
    return
  fi
  if p="$(command -v mfp-mcp 2>/dev/null)" && [ -n "$p" ]; then
    case "$p" in /*)
      printf '%s\n' "$p"
      return
      ;;
    esac
  fi
  if [ -x "$MFP_HOME/.local/bin/mfp-mcp" ]; then
    printf '%s\n' "$MFP_HOME/.local/bin/mfp-mcp"
    return
  fi
  if [ "$DRY_RUN" = 1 ]; then
    warn "mfp-mcp not found; using a placeholder path for the dry run."
    printf '%s\n' "$MFP_HOME/.local/bin/mfp-mcp"
    return
  fi
  die "mfp-mcp not found. Run scripts/install-mac.sh first."
}

# resolve_tool_python : the Python inside Mason's uv tool environment.
resolve_tool_python() {
  local d
  if [ -n "${MFP_TOOL_PY:-}" ]; then
    printf '%s\n' "$MFP_TOOL_PY"
    return
  fi
  if have uv && d="$(uv tool dir 2>/dev/null)" && [ -x "$d/mfp-mcp/bin/python" ]; then
    printf '%s\n' "$d/mfp-mcp/bin/python"
    return
  fi
  printf '%s\n' "$MFP_HOME/.local/share/uv/tools/mfp-mcp/bin/python"
}

# --- Mason session metadata (never prints secrets) ------------------------------
mason_cookies_file() { printf '%s/cookies.json' "$MFP_MCP_CONFIG_DIR"; }

# mason_saved_username : username persisted by a successful `mfp-mcp auth`.
mason_saved_username() {
  local f
  f="$(mason_cookies_file)"
  [ -f "$f" ] || return 1
  plutil -extract username raw -o - "$f" 2>/dev/null
}

# session_token_hash : first 8 hex chars of sha256(session token), or "none".
session_token_hash() {
  local f tok
  f="$(mason_cookies_file)"
  if [ -f "$f" ] && tok="$(plutil -extract 'cookies.__Secure-next-auth\.session-token' raw -o - "$f" 2>/dev/null)" && [ -n "$tok" ]; then
    printf '%s' "$tok" | shasum -a 256 | cut -c1-8
  else
    printf 'none\n'
  fi
}

# configured_username : MFP_USERNAME from env, mason.env, or Mason's saved
# session, in that order. Prints nothing and returns 1 if none.
configured_username() {
  local u="${MFP_USERNAME:-}"
  if [ -z "$u" ] && [ -f "$MFP_CONFIG_DIR/mason.env" ]; then
    u="$(load_env_file "$MFP_CONFIG_DIR/mason.env" MFP_USERNAME; printf '%s' "${MFP_USERNAME:-}")"
  fi
  [ -n "$u" ] || u="$(mason_saved_username 2>/dev/null || true)"
  [ -n "$u" ] || return 1
  printf '%s\n' "$u"
}

USERNAME_HINT="SSO/email MyFitnessPal logins need MFP_USERNAME (your MFP username, not email): put MFP_USERNAME=<name> in ~/.config/mfp-mason/mason.env (chmod 600), then run: bin/mfpctl restart"

# --- env files --------------------------------------------------------------
# load_env_file FILE KEY... : export only the listed keys from a KEY=VALUE
# file. The file is parsed, never sourced, so it can't run code. Surrounding
# single/double quotes on values are stripped. Values are never printed.
load_env_file() {
  local file="$1"
  shift
  local line key val allowed
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | '#'*) continue ;; esac
    line="${line#export }"
    key="${line%%=*}"
    val="${line#*=}"
    [ "$key" = "$line" ] && continue
    for allowed in "$@"; do
      if [ "$key" = "$allowed" ]; then
        case "$val" in
          \"*\") val="${val#\"}" val="${val%\"}" ;;
          \'*\') val="${val#\'}" val="${val%\'}" ;;
        esac
        export "$key=$val"
      fi
    done
  done <"$file"
}

# Non-secret Mason tuning, rendered into the service at install time.
MASON_ENV_KEYS="MFP_IMPERSONATE MFP_USERNAME MFP_SYNC_DAYS"

load_mason_env() {
  local f="$MFP_CONFIG_DIR/mason.env"
  # shellcheck disable=SC2086  # word-splitting the key list is intended
  [ -f "$f" ] && load_env_file "$f" $MASON_ENV_KEYS
  return 0
}

# --- templates ----------------------------------------------------------------
xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  printf '%s' "$s"
}

# render_template FILE NAME=VALUE... : replace @NAME@ with VALUE (verbatim; the
# caller escapes) and fail if any @PLACEHOLDER@ is left over.
render_template() {
  local file="$1"
  shift
  [ -f "$file" ] || die "template not found: $file"
  local content kv name val
  content="$(cat "$file")"
  for kv in "$@"; do
    name="${kv%%=*}"
    val="${kv#*=}"
    content="${content//@${name}@/$val}"
  done
  if printf '%s' "$content" | grep -Eq '@[A-Z][A-Z0-9_]*@'; then
    die "unreplaced placeholder in $(basename "$file"): $(printf '%s' "$content" | grep -Eo '@[A-Z][A-Z0-9_]*@' | sort -u | tr '\n' ' ')"
  fi
  printf '%s\n' "$content"
}

# --- MCP probe ----------------------------------------------------------------
# mcp_initialize : POST an MCP initialize to Mason. On success prints
# "NAME VERSION" and returns 0; returns 1 if unreachable or not MCP.
# Handles both JSON and SSE (text/event-stream) responses.
mcp_initialize() {
  local body tmp name version
  body="$(curl -sS --max-time "${MCP_PROBE_TIMEOUT:-10}" -X POST \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mfpctl","version":"1"}}}' \
    "$MFP_MCP_URL" 2>/dev/null)" || return 1
  if printf '%s\n' "$body" | grep -q '^data:'; then
    body="$(printf '%s\n' "$body" | sed -n 's/^data:[[:space:]]*//p' | head -n 1)"
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/mfp-init.XXXXXX")"
  printf '%s' "$body" >"$tmp"
  name="$(plutil -extract result.serverInfo.name raw -o - "$tmp" 2>/dev/null)" || {
    rm -f "$tmp"
    return 1
  }
  version="$(plutil -extract result.serverInfo.version raw -o - "$tmp" 2>/dev/null || echo '?')"
  rm -f "$tmp"
  printf '%s %s\n' "$name" "$version"
}
