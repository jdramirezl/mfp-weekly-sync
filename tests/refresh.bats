#!/usr/bin/env bats
# scripts/refresh-session.sh and com.jdrami.mfp-refresh.

load helpers

OLD_TOKEN="old-SECRET-token-value-1111"
NEW_TOKEN="new-SECRET-token-value-2222"

setup() {
  common_setup
  REFRESH="$REPO_ROOT/scripts/refresh-session.sh"
  MFPCTL="$REPO_ROOT/bin/mfpctl"
  make_fake_mfp_mcp
  MASON_CFG="$TEST_HOME/Library/Application Support/myfitnesspal-mcp"
  mkdir -p "$MFP_MCP_DATA_DIR/browser-profile" "$MASON_CFG"
  write_cookies "$OLD_TOKEN"
  # Healthy Mason for the follow-up health check.
  stub curl 0 '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"myfitnesspal","version":"1"}}}'
  # Stub of Mason's tool Python. $STUB_DIR/rotate-to: write that token;
  # $STUB_DIR/refresh.rc: exit code. Records the launchd state it ran under.
  export MFP_TOOL_PY="$TEST_HOME/tool/bin/python"
  mkdir -p "$(dirname "$MFP_TOOL_PY")"
  cat >"$MFP_TOOL_PY" <<'EOF'
#!/bin/bash
echo "tool-python refresh MFP_USERNAME=${MFP_USERNAME:-} mason_loaded=$([ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ] && echo yes || echo no)" >>"$STUB_LOG"
if [ -f "$STUB_DIR/rotate-to" ]; then
  printf '{"cookies":{"__Secure-next-auth.session-token":"%s"},"username":"jdr"}' "$(cat "$STUB_DIR/rotate-to")" >"$MASON_CFG/cookies.json"
fi
exit "$(cat "$STUB_DIR/refresh.rc" 2>/dev/null || echo 0)"
EOF
  chmod +x "$MFP_TOOL_PY"
  export MASON_CFG
  mkdir -p "$LAUNCH_AGENTS_DIR" && touch "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
}

write_cookies() {
  printf '{"cookies":{"__Secure-next-auth.session-token":"%s"},"username":"jdr"}' "$1" >"$MASON_CFG/cookies.json"
}

hash8() { printf '%s' "$1" | shasum -a 256 | cut -c1-8; }

@test "a rotated token logs rotated: yes with hashes only" {
  echo "$NEW_TOKEN" >"$STUB_DIR/rotate-to"
  run "$REFRESH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"rotated: yes ($(hash8 "$OLD_TOKEN") -> $(hash8 "$NEW_TOKEN"))"* ]] || false
  grep -q "rotated: yes" "$MFP_LOG_DIR/refresh.log"
}

@test "an unchanged token logs rotated: no and still succeeds" {
  run "$REFRESH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"rotated: no (token unchanged, $(hash8 "$OLD_TOKEN"))"* ]] || false
}

@test "token values never appear in output, logs or argv" {
  echo "$NEW_TOKEN" >"$STUB_DIR/rotate-to"
  run "$REFRESH"
  [[ "$output" != *SECRET* ]] || false
  ! grep -rq SECRET "$MFP_LOG_DIR" || false
  ! grep -q SECRET "$STUB_LOG" || false
}

@test "Mason is stopped during the refresh and restarted after" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$REFRESH"
  [ "$status" -eq 0 ]
  grep -q 'tool-python refresh MFP_USERNAME= mason_loaded=no' "$STUB_LOG"
  run grep -n -E 'launchctl (bootout|bootstrap)|tool-python' "$STUB_LOG"
  [[ "${lines[0]}" == *"bootout gui/$(id -u)/com.jdrami.mfp-mcp" ]] || false
  [[ "${lines[1]}" == *"tool-python refresh"* ]] || false
  [[ "${lines[2]}" == *"bootstrap gui/$(id -u) $LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist" ]] || false
  [ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ]
}

@test "Mason is restarted even when the refresh fails" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  echo 1 >"$STUB_DIR/refresh.rc"
  run "$REFRESH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"refresh FAILED (exit 1)"* ]] || false
  grep -q "launchctl bootstrap gui/$(id -u) " "$STUB_LOG"
  [ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ]
}

@test "a Mason that was not running is left stopped" {
  run "$REFRESH"
  [ "$status" -eq 0 ]
  ! grep -q 'launchctl boot' "$STUB_LOG" || false
}

@test "MFP_USERNAME from mason.env is passed to the refresh" {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_USERNAME=jdramirezl\n' >"$MFP_CONFIG_DIR/mason.env"
  run "$REFRESH"
  grep -q 'tool-python refresh MFP_USERNAME=jdramirezl' "$STUB_LOG"
}

@test "the health check runs afterwards" {
  run "$REFRESH"
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
  [[ "$output" == *"health: ok"* ]] || false
}

@test "an unseeded profile fails clearly without touching Mason" {
  rm -rf "$MFP_MCP_DATA_DIR/browser-profile"
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$REFRESH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no seeded browser profile"* ]] || false
  [[ "$output" == *"pbpaste | bin/mfp-auth"* ]] || false
  ! grep -q 'launchctl bootout' "$STUB_LOG" || false
}

@test "a missing session token after refresh is a failure" {
  printf '' >"$STUB_DIR/rotate-to" # refresh saved cookies without a token
  run "$REFRESH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no session token saved"* ]] || false
}

@test "--dry-run changes nothing" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$MFPCTL" refresh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ launchctl bootout"* ]] || false
  ! grep -q 'tool-python' "$STUB_LOG" || false
  ! grep -q 'launchctl boot' "$STUB_LOG" || false
  [ ! -e "$MFP_LOG_DIR/refresh.log" ]
}

@test "refresh plist is weekly (Sunday 04:30), no RunAtLoad, no KeepAlive" {
  rm "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
  "$MFPCTL" install
  p="$LAUNCH_AGENTS_DIR/com.jdrami.mfp-refresh.plist"
  plutil -lint "$p"
  [ "$(plutil -extract StartCalendarInterval.Weekday raw -o - "$p")" = 0 ]
  [ "$(plutil -extract StartCalendarInterval.Hour raw -o - "$p")" = 4 ]
  [ "$(plutil -extract StartCalendarInterval.Minute raw -o - "$p")" = 30 ]
  run plutil -extract RunAtLoad raw -o - "$p"
  [ "$status" -ne 0 ]
  run plutil -extract KeepAlive raw -o - "$p"
  [ "$status" -ne 0 ]
  [ "$(plutil -extract EnvironmentVariables.MFP_TOOL_PY raw -o - "$p")" = "$MFP_TOOL_PY" ]
  [ -x "$MFP_STATE_DIR/bin/refresh-session.sh" ]
}

@test "install loads 3 agents (not the tunnel); uninstall removes all 4" {
  rm "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
  "$MFPCTL" install
  [ "$(grep -c '^launchctl bootstrap' "$STUB_LOG")" = 3 ]
  for l in mfp-mcp mfp-health mfp-refresh; do grep -q "bootstrap gui/$(id -u) .*com.jdrami.$l.plist" "$STUB_LOG"; done
  touch "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-tunnel.plist" "$STUB_DIR/loaded-com.jdrami.mfp-tunnel"
  : >"$STUB_LOG"
  "$MFPCTL" uninstall
  [ "$(grep -c '^launchctl bootout' "$STUB_LOG")" = 4 ]
  [ -z "$(ls -A "$LAUNCH_AGENTS_DIR")" ]
}

@test "status lists the refresh agent" {
  rm "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
  "$MFPCTL" install
  run "$MFPCTL" status
  [[ "$output" == *"com.jdrami.mfp-refresh"*"loaded"* ]] || false
}

@test "refresh waits for Mason's bootout and still exits 0 if the restart can't register" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  echo 3 >"$STUB_DIR/bootout-lag"
  echo 99 >"$STUB_DIR/bootstrap-silent-n"
  run "$REFRESH" --no-health
  [ "$status" -eq 0 ]
  grep -q 'tool-python refresh MFP_USERNAME= mason_loaded=no' "$STUB_LOG"
  [[ "$output" == *"could not register com.jdrami.mfp-mcp"*"launchctl bootstrap gui/"* ]] || false
}
