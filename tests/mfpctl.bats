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

@test "ProgramArguments are absolute and bound to 127.0.0.1:8484" {
  "$MFPCTL" install
  [ "$(plist_get "$MASON_PLIST" ProgramArguments.0)" = "$MFP_MCP_BIN" ]
  [[ "$(plist_get "$MASON_PLIST" ProgramArguments.0)" == /* ]]
  [ "$(plist_get "$MASON_PLIST" ProgramArguments.1)" = --http ]
  [ "$(plist_get "$MASON_PLIST" ProgramArguments.3)" = 127.0.0.1 ]
  [ "$(plist_get "$MASON_PLIST" ProgramArguments.5)" = 8484 ]
  [ "$(plist_get "$MASON_PLIST" KeepAlive)" = true ]
  [ "$(plist_get "$MASON_PLIST" RunAtLoad)" = true ]
  [ "$(plist_get "$MASON_PLIST" ThrottleInterval)" = 30 ]
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_MCP_DATA_DIR)" = "$MFP_MCP_DATA_DIR" ]
  [[ "$(plist_get "$MASON_PLIST" StandardErrorPath)" == "$MFP_LOG_DIR/"* ]]
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

@test "optional Mason env keys are absent unless configured" {
  "$MFPCTL" install
  run plist_get "$MASON_PLIST" EnvironmentVariables.MFP_IMPERSONATE
  [ "$status" -ne 0 ]
  run plist_get "$MASON_PLIST" EnvironmentVariables.MFP_USERNAME
  [ "$status" -ne 0 ]
}

@test "mason.env values are rendered (and only allowed keys)" {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_IMPERSONATE=chrome124\nMFP_USERNAME="a&b"\nMFP_COOKIE=secret-cookie\n' >"$MFP_CONFIG_DIR/mason.env"
  "$MFPCTL" install
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_IMPERSONATE)" = chrome124 ]
  [ "$(plist_get "$MASON_PLIST" EnvironmentVariables.MFP_USERNAME)" = 'a&b' ]
  ! grep -q secret-cookie "$MASON_PLIST"
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
