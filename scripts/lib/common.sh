#!/bin/bash
# shellcheck disable=SC2034  # constants are consumed by the scripts that source this
# Shared helpers for mfpctl and the scripts. Sourced, never executed.
# Must stay compatible with macOS's stock /bin/bash 3.2: no associative arrays,
# no mapfile, no ${var,,}.

# --- constants ------------------------------------------------------------
MFP_MCP_VERSION="0.4.1"
MFP_MCP_SPEC="mfp-mcp[autorefresh]==${MFP_MCP_VERSION}"
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
  MFP_STATE_DIR="${MFP_STATE_DIR:-$MFP_HOME/Library/Application Support/mfp-mason}"
  MFP_CONFIG_DIR="${MFP_CONFIG_DIR:-$MFP_HOME/.config/mfp-mason}"
  MFP_LOG_DIR="${MFP_LOG_DIR:-$MFP_HOME/Library/Logs/mfp-mason}"
  MFP_HOST="${MFP_HOST:-$REQUIRED_HOST}"
  MFP_PORT="${MFP_PORT:-8484}"
  TUNNEL_HEALTH_ADDR="${TUNNEL_HEALTH_ADDR:-127.0.0.1:8080}"
  MFP_MCP_URL="http://${REQUIRED_HOST}:${MFP_PORT}/mcp"
  export MFP_HOME LAUNCH_AGENTS_DIR MFP_MCP_DATA_DIR MFP_STATE_DIR MFP_CONFIG_DIR \
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

# env_entries KEY... : <key>/<string> plist lines for each KEY that is set.
env_entries() {
  local key val out=""
  for key in "$@"; do
    val="${!key:-}"
    [ -n "$val" ] || continue
    out="${out}
    <key>${key}</key><string>$(xml_escape "$val")</string>"
  done
  printf '%s' "$out"
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
