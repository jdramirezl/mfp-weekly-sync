#!/bin/bash
# LaunchAgent entry point for the optional OpenAI tunnel-client, which lets
# ChatGPT reach the shared local Mason instance without exposing it.
#
# Flags checked against github.com/openai/tunnel-client (README,
# docs/configuration.md, docs/troubleshooting.md, client 0.0.17-dev):
#   run                           daemon mode; reads CONTROL_PLANE_API_KEY and
#                                 CONTROL_PLANE_TUNNEL_ID from the environment
#                                 (no profile or `init` needed).
#   --mcp.server-url URL          HTTP-streamable upstream (env MCP_SERVER_URL).
#   --health.listen-addr ADDR     /healthz, /readyz, /ui; default 127.0.0.1:8080.
#   --mcp.startup-wait-timeout D  hold the first poll until Mason's listener
#                                 accepts connections (both start at login).
# Secrets are read from tunnel.env (must be 0600) and passed via the
# environment only, never argv, so they don't appear in `ps` or the plist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

mfp_init_paths
ENV_FILE="$MFP_CONFIG_DIR/tunnel.env"

[ -f "$ENV_FILE" ] || die "missing $ENV_FILE (copy tunnel.env.example and fill it in)"
mode="$(file_mode "$ENV_FILE")"
case "$mode" in 600 | 400) ;; *) die "$ENV_FILE has mode $mode; run: chmod 600 \"$ENV_FILE\"" ;; esac

CONTROL_PLANE_TUNNEL_ID="" CONTROL_PLANE_API_KEY=""
load_env_file "$ENV_FILE" CONTROL_PLANE_TUNNEL_ID CONTROL_PLANE_API_KEY
[ -n "$CONTROL_PLANE_TUNNEL_ID" ] || die "CONTROL_PLANE_TUNNEL_ID is empty in $ENV_FILE"
[ -n "$CONTROL_PLANE_API_KEY" ] || die "CONTROL_PLANE_API_KEY is empty in $ENV_FILE"
case "$CONTROL_PLANE_TUNNEL_ID" in tunnel_*) ;; *) die "CONTROL_PLANE_TUNNEL_ID should look like tunnel_..." ;; esac
case "$TUNNEL_HEALTH_ADDR" in 127.0.0.1:*) ;; *) die "TUNNEL_HEALTH_ADDR must stay on 127.0.0.1" ;; esac

BIN="${TUNNEL_CLIENT_BIN:-$(command -v tunnel-client 2>/dev/null || true)}"
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then die "tunnel-client not found (brew install openai/tools/tunnel-client)"; fi

exec "$BIN" run \
  --mcp.server-url "$MFP_MCP_URL" \
  --health.listen-addr "$TUNNEL_HEALTH_ADDR" \
  --mcp.startup-wait-timeout 120s
