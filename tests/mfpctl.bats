#!/usr/bin/env bats
# bin/mfpctl core: rendering, install/uninstall, start/stop, status.

load helpers

setup() {
  common_setup
  MFPCTL="$REPO_ROOT/bin/mfpctl"
  make_fake_mfp_mcp
  MASON_PLIST="$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
}

plist_get() { plutil -extract "$2" raw -o - "$1"; }

@test "install renders a lint-clean Mason plist and bootstraps it" {
  run "$MFPCTL" install
  [ "$status" -eq 0 ]
  [ -f "$MASON_PLIST" ]
  run plutil -lint "$MASON_PLIST"
  [ "$status" -eq 0 ]
  grep -qx "launchctl bootstrap gui/$(id -u) $MASON_PLIST" "$STUB_LOG"
  [ "$(file_mode "$MFP_LOG_DIR")" = 700 ]
}

@test "plist runs the mason-run.sh wrapper with absolute paths" {
  "$MFPCTL" install
  [ "$(plist_get "$MASON_PLIST" ProgramArguments.0)" = /bin/bash ]
  [ "$(plist_get "$MASON_PLIST" ProgramArguments.1)" = "$MFP_STATE_DIR/bin/mason-run.sh" ]
  [ -x "$MFP_STATE_DIR/bin/mason-run.sh" ]
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_MCP_BIN)" = "$MFP_MCP_BIN" ]
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_PORT)" = 8484 ]
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_CONFIG_DIR)" = "$MFP_CONFIG_DIR" ]
  [ "$(plist_get "$MASON_PLIST" KeepAlive)" = true ]
  [ "$(plist_get "$MASON_PLIST" RunAtLoad)" = true ]
  [ "$(plist_get "$MASON_PLIST" ThrottleInterval)" = 30 ]
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_MCP_DATA_DIR)" = "$MFP_MCP_DATA_DIR" ]
  [[ "$(plist_get "$MASON_PLIST" StandardErrorPath)" == "$MFP_LOG_DIR/"* ]]
}

# Run the installed wrapper the way launchd would: only the plist's env.
run_wrapper() {
  env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin STUB_LOG="$STUB_LOG" STUB_DIR="$STUB_DIR" \
    MFP_HOME_OVERRIDE="$TEST_HOME" MFP_MCP_BIN="$MFP_MCP_BIN" MFP_MCP_DATA_DIR="$MFP_MCP_DATA_DIR" \
    MFP_CONFIG_DIR="$MFP_CONFIG_DIR" MFP_PORT=8484 "$@" /bin/bash "$MFP_STATE_DIR/bin/mason-run.sh"
}

@test "wrapper execs mfp-mcp --http on 127.0.0.1:8484" {
  "$MFPCTL" install
  run run_wrapper
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/serve-argv")" = "--http --host 127.0.0.1 --port 8484" ]
  grep -qx "MFP_MCP_DATA_DIR=$MFP_MCP_DATA_DIR" "$STUB_DIR/serve-env"
}

@test "wrapper refuses a non-loopback host" {
  "$MFPCTL" install
  run run_wrapper MFP_HOST=0.0.0.0
  [ "$status" -ne 0 ]
  [[ "$output" == *"must bind 127.0.0.1 only"* ]]
  [ ! -e "$STUB_DIR/serve-argv" ]
}

@test "wrapper loads mason.env at start and ignores MFP_COOKIE" {
  "$MFPCTL" install
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_IMPERSONATE=chrome124\nMFP_USERNAME="jdramirezl"\nMFP_COOKIE=secret-cookie\n' >"$MFP_CONFIG_DIR/mason.env"
  chmod 600 "$MFP_CONFIG_DIR/mason.env"
  run run_wrapper
  [ "$status" -eq 0 ]
  grep -qx 'MFP_IMPERSONATE=chrome124' "$STUB_DIR/serve-env"
  grep -qx 'MFP_USERNAME=jdramirezl' "$STUB_DIR/serve-env"
  ! grep -q secret-cookie "$STUB_DIR/serve-env"
  [[ "$output" != *"no MFP_USERNAME"* ]]
}

@test "wrapper warns about a loose mason.env and a missing username" {
  "$MFPCTL" install
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_IMPERSONATE=chrome124\n' >"$MFP_CONFIG_DIR/mason.env"
  chmod 644 "$MFP_CONFIG_DIR/mason.env"
  run run_wrapper
  [ "$status" -eq 0 ]
  [[ "$output" == *"has mode 644"* ]]
  [[ "$output" == *"no MFP_USERNAME"* ]]
}

@test "service PATH is minimal plus the mfp-mcp bin dir" {
  "$MFPCTL" install
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.PATH)" = "/usr/bin:/bin:/usr/sbin:/sbin:$(dirname "$MFP_MCP_BIN")" ]
}

@test "refuses any host other than 127.0.0.1" {
  MFP_HOST=0.0.0.0 run "$MFPCTL" install
  [ "$status" -ne 0 ]
  [[ "$output" == *"must bind 127.0.0.1 only"* ]]
  [ ! -e "$MASON_PLIST" ]
}

@test "rejects a non-numeric port" {
  MFP_PORT='8484<x>' run "$MFPCTL" install
  [ "$status" -ne 0 ]
}

@test "the plist never bakes in mason.env values" {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_IMPERSONATE=chrome124\nMFP_USERNAME=jdr\nMFP_COOKIE=secret-cookie\n' >"$MFP_CONFIG_DIR/mason.env"
  "$MFPCTL" install
  run plist_get "$MASON_PLIST" EnvironmentVariables.MFP_USERNAME
  [ "$status" -ne 0 ]
  run plist_get "$MASON_PLIST" EnvironmentVariables.MFP_IMPERSONATE
  [ "$status" -ne 0 ]
  ! grep -q secret-cookie "$MASON_PLIST"
}

@test "install warns when no username is configured anywhere" {
  run "$MFPCTL" install
  [ "$status" -eq 0 ]
  [[ "$output" == *"No MFP_USERNAME found"* ]]
}

@test "install is quiet about the username when mason.env has it" {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_USERNAME=jdr\n' >"$MFP_CONFIG_DIR/mason.env"
  run "$MFPCTL" install
  [[ "$output" != *"No MFP_USERNAME found"* ]]
}

@test "install is quiet about the username when Mason saved one" {
  mkdir -p "$TEST_HOME/Library/Application Support/myfitnesspal-mcp"
  printf '{"cookies":{},"username":"jdr"}' >"$TEST_HOME/Library/Application Support/myfitnesspal-mcp/cookies.json"
  run "$MFPCTL" install
  [[ "$output" != *"No MFP_USERNAME found"* ]]
}

@test "reinstall boots out the loaded service before bootstrapping" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$MFPCTL" install
  [ "$status" -eq 0 ]
  run grep -n -E 'launchctl (bootout|bootstrap)' "$STUB_LOG"
  [[ "${lines[0]}" == *"bootout gui/$(id -u)/com.jdrami.mfp-mcp" ]]
  [[ "${lines[1]}" == *"bootstrap gui/$(id -u) $MASON_PLIST" ]]
}

@test "install fails clearly when mfp-mcp is missing" {
  unset MFP_MCP_BIN
  rm -f "$TEST_HOME/.local/bin/mfp-mcp"
  run "$MFPCTL" install
  [ "$status" -ne 0 ]
  [[ "$output" == *"Run scripts/install-mac.sh first"* ]]
}

@test "install --dry-run prints the plist and launchctl calls, writes nothing" {
  unset MFP_MCP_BIN
  run "$MFPCTL" install --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"--- would write $MASON_PLIST ---"* ]]
  [[ "$output" == *"<string>127.0.0.1</string>"* ]]
  [[ "$output" == *"+ launchctl bootstrap gui/"* ]]
  [ ! -e "$LAUNCH_AGENTS_DIR" ]
  [ ! -e "$MFP_LOG_DIR" ]
  ! grep -q '^launchctl boot' "$STUB_LOG"
}

@test "uninstall boots out and removes plists but keeps data" {
  "$MFPCTL" install
  mkdir -p "$MFP_MCP_DATA_DIR" && touch "$MFP_MCP_DATA_DIR/data.db"
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$MFPCTL" uninstall
  [ "$status" -eq 0 ]
  [ ! -e "$MASON_PLIST" ]
  grep -qx "launchctl bootout gui/$(id -u)/com.jdrami.mfp-mcp" "$STUB_LOG"
  [ -e "$MFP_MCP_DATA_DIR/data.db" ]
}

@test "start bootstraps when unloaded, kickstarts when loaded" {
  "$MFPCTL" install
  : >"$STUB_LOG"
  "$MFPCTL" start
  grep -q "launchctl bootstrap gui/" "$STUB_LOG"
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  : >"$STUB_LOG"
  "$MFPCTL" start
  grep -qx "launchctl kickstart gui/$(id -u)/com.jdrami.mfp-mcp" "$STUB_LOG"
}

@test "restart uses kickstart -k; stop uses bootout" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  "$MFPCTL" restart
  grep -qx "launchctl kickstart -k gui/$(id -u)/com.jdrami.mfp-mcp" "$STUB_LOG"
  "$MFPCTL" stop
  grep -qx "launchctl bootout gui/$(id -u)/com.jdrami.mfp-mcp" "$STUB_LOG"
}

@test "status parses an SSE initialize response" {
  "$MFPCTL" install
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  stub launchctl 0 $'\tstate = running\n\tpid = 4242\n'
  stub curl 0 $'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"myfitnesspal","version":"1.20.0"}}}\n\n'
  run "$MFPCTL" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"com.jdrami.mfp-mcp"*"loaded (state=running, pid=4242)"* ]]
  [[ "$output" == *"MCP http://127.0.0.1:8484/mcp: OK (myfitnesspal 1.20.0)"* ]]
  grep -q 'Accept: application/json, text/event-stream' "$STUB_LOG"
}

@test "status parses a plain JSON initialize response" {
  stub curl 0 '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"myfitnesspal","version":"9"}}}'
  run "$MFPCTL" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"OK (myfitnesspal 9)"* ]]
}

@test "status fails cleanly when Mason is unreachable" {
  stub curl 7 ''
  run "$MFPCTL" status
  [ "$status" -eq 1 ]
  [[ "$output" == *"UNREACHABLE"* ]]
  [[ "$output" == *"not installed"* ]]
}

@test "unknown command fails with usage" {
  run "$MFPCTL" frobnicate
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage: mfpctl"* ]]
}
