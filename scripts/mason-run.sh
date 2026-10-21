#!/bin/bash
# LaunchAgent entry point for Mason. Loads ~/.config/mfp-mason/mason.env at
# every start, so editing it only needs `mfpctl restart`, then execs Mason on
# loopback. Only allowlisted, non-secret keys are read; MFP_COOKIE is ignored.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

mfp_init_paths
require_loopback_host

ENV_FILE="$MFP_CONFIG_DIR/mason.env"
if [ -f "$ENV_FILE" ]; then
  mode="$(file_mode "$ENV_FILE")"
  case "$mode" in 600 | 400) ;; *) warn "$ENV_FILE has mode $mode; run: chmod 600 \"$ENV_FILE\"" ;; esac
  load_mason_env
fi
[ -n "${MFP_USERNAME:-}" ] || mason_saved_username >/dev/null 2>&1 || warn "no MFP_USERNAME. $USERNAME_HINT"

BIN="${MFP_MCP_BIN:-}"
case "$BIN" in /*) ;; *) die "MFP_MCP_BIN must be an absolute path (got '$BIN')" ;; esac
[ -x "$BIN" ] || die "$BIN is not executable; re-run scripts/install-mac.sh"
case "$MFP_PORT" in '' | *[!0-9]*) die "MFP_PORT must be a number" ;; esac

export MFP_MCP_DATA_DIR MFP_LOG_DIR
# Default: Mason's own Python runs mason_entry.py, which loads the cookie-
# rotation shim (logs MFP Set-Cookie rotations; persists them only with
# MFP_PERSIST_ROTATIONS=1) and then Mason's CLI. MFP_COOKIE_SHIM=0 = plain binary.
if [ "${MFP_COOKIE_SHIM:-1}" != 0 ] && [ -f "$SCRIPT_DIR/mason_entry.py" ] && PY="$(mason_python "$BIN")"; then
  exec "$PY" "$SCRIPT_DIR/mason_entry.py" --http --host "$REQUIRED_HOST" --port "$MFP_PORT"
fi
exec "$BIN" --http --host "$REQUIRED_HOST" --port "$MFP_PORT"
