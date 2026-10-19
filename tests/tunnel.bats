#!/usr/bin/env bats
# Optional tunnel service: mfpctl tunnel enable|disable and scripts/tunnel-run.sh.

load helpers

TUNNEL_ID="tunnel_0123456789abcdef0123456789abcdef"
API_KEY="sk-proj-SUPERSECRETKEY"

setup() {
  common_setup
  MFPCTL="$REPO_ROOT/bin/mfpctl"
  make_fake_mfp_mcp
  TUNNEL_PLIST="$LAUNCH_AGENTS_DIR/com.jdrami.mfp-tunnel.plist"
  # Fake tunnel-client: records argv and the two env vars it should receive.
  mkdir -p "$TEST_HOME/brew/bin"
  cat >"$TEST_HOME/brew/bin/tunnel-client" <<'EOF'
#!/bin/bash
printf 'tunnel-client' >>"$STUB_LOG"; for a in "$@"; do printf ' %s' "$a" >>"$STUB_LOG"; done; printf '\n' >>"$STUB_LOG"
printf 'ENV %s %s\n' "$CONTROL_PLANE_TUNNEL_ID" "$CONTROL_PLANE_API_KEY" >"$STUB_DIR/tunnel-env"
EOF
  chmod +x "$TEST_HOME/brew/bin/tunnel-client"
  export TUNNEL_CLIENT_BIN="$TEST_HOME/brew/bin/tunnel-client"
}

write_tunnel_env() {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'CONTROL_PLANE_TUNNEL_ID=%s\nCONTROL_PLANE_API_KEY="%s"\n' "$TUNNEL_ID" "$API_KEY" >"$MFP_CONFIG_DIR/tunnel.env"
  chmod "${1:-600}" "$MFP_CONFIG_DIR/tunnel.env"
}

@test "mfpctl install does not install the tunnel" {
  "$MFPCTL" install
  [ ! -e "$TUNNEL_PLIST" ]
  ! grep -q mfp-tunnel "$STUB_LOG" || false
}

@test "enable without tunnel.env fails clearly" {
  run "$MFPCTL" tunnel enable
  [ "$status" -ne 0 ]
  [[ "$output" == *"missing $MFP_CONFIG_DIR/tunnel.env"* ]] || false
  [ ! -e "$TUNNEL_PLIST" ]
}

@test "enable refuses a world-readable tunnel.env" {
  write_tunnel_env 644
  run "$MFPCTL" tunnel enable
  [ "$status" -ne 0 ]
  [[ "$output" == *"mode 644"* ]] || false
  [ ! -e "$TUNNEL_PLIST" ]
}

@test "enable refuses an empty API key" {
  write_tunnel_env
  printf 'CONTROL_PLANE_TUNNEL_ID=%s\nCONTROL_PLANE_API_KEY=\n' "$TUNNEL_ID" >"$MFP_CONFIG_DIR/tunnel.env"
  run "$MFPCTL" tunnel enable
  [ "$status" -ne 0 ]
  [[ "$output" == *"CONTROL_PLANE_API_KEY is empty"* ]] || false
}

@test "enable fails clearly when tunnel-client is missing" {
  write_tunnel_env
  unset TUNNEL_CLIENT_BIN
  run "$MFPCTL" tunnel enable
  [ "$status" -ne 0 ]
  [[ "$output" == *"install-mac.sh --with-tunnel"* ]] || false
}

@test "enable renders a lint-clean, secret-free plist and bootstraps it" {
  write_tunnel_env
  run "$MFPCTL" tunnel enable
  [ "$status" -eq 0 ]
  plutil -lint "$TUNNEL_PLIST"
  ! grep -qF "$API_KEY" "$TUNNEL_PLIST" || false
  ! grep -qF "$TUNNEL_ID" "$TUNNEL_PLIST" || false
  ! grep -q CONTROL_PLANE "$TUNNEL_PLIST" || false
  [ "$(plutil -extract KeepAlive raw -o - "$TUNNEL_PLIST")" = true ]
  [ "$(plutil -extract EnvironmentVariables.TUNNEL_HEALTH_ADDR raw -o - "$TUNNEL_PLIST")" = 127.0.0.1:8080 ]
  [ -x "$MFP_STATE_DIR/bin/tunnel-run.sh" ]
  grep -qx "launchctl bootstrap gui/$(id -u) $TUNNEL_PLIST" "$STUB_LOG"
}

@test "wrapper passes the 127.0.0.1 Mason URL in argv and secrets only via env" {
  write_tunnel_env
  "$MFPCTL" tunnel enable
  : >"$STUB_LOG"
  run env MFP_CONFIG_DIR="$MFP_CONFIG_DIR" TUNNEL_CLIENT_BIN="$TUNNEL_CLIENT_BIN" \
    /bin/bash "$MFP_STATE_DIR/bin/tunnel-run.sh"
  [ "$status" -eq 0 ]
  grep -qx 'tunnel-client run --mcp.server-url http://127.0.0.1:8484/mcp --health.listen-addr 127.0.0.1:8080 --mcp.startup-wait-timeout 120s' "$STUB_LOG"
  ! grep -qF "$API_KEY" "$STUB_LOG" || false
  [ "$(cat "$STUB_DIR/tunnel-env")" = "ENV $TUNNEL_ID $API_KEY" ]
}

@test "wrapper refuses loose permissions and bad tunnel ids at runtime" {
  write_tunnel_env 644
  run /bin/bash "$REPO_ROOT/scripts/tunnel-run.sh"
  [ "$status" -ne 0 ]
  [[ "$output" == *"mode 644"* ]] || false
  printf 'CONTROL_PLANE_TUNNEL_ID=nope\nCONTROL_PLANE_API_KEY=x\n' >"$MFP_CONFIG_DIR/tunnel.env"
  chmod 600 "$MFP_CONFIG_DIR/tunnel.env"
  run /bin/bash "$REPO_ROOT/scripts/tunnel-run.sh"
  [ "$status" -ne 0 ]
  [[ "$output" == *"tunnel_"* ]] || false
  [ ! -e "$STUB_DIR/tunnel-env" ]
}

@test "disable boots out and removes the tunnel plist" {
  write_tunnel_env
  "$MFPCTL" tunnel enable
  touch "$STUB_DIR/loaded-com.jdrami.mfp-tunnel"
  run "$MFPCTL" tunnel disable
  [ "$status" -eq 0 ]
  [ ! -e "$TUNNEL_PLIST" ]
  grep -qx "launchctl bootout gui/$(id -u)/com.jdrami.mfp-tunnel" "$STUB_LOG"
}

@test "enable --dry-run shows the plist and changes nothing" {
  write_tunnel_env
  run "$MFPCTL" tunnel enable --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"--- would write $TUNNEL_PLIST ---"* ]] || false
  [[ "$output" == *"+ launchctl bootstrap gui/"* ]] || false
  [[ "$output" != *"$API_KEY"* ]] || false
  [ ! -e "$TUNNEL_PLIST" ]
}

@test "tunnel status reports readiness" {
  stub curl 0 ''
  run "$MFPCTL" tunnel status
  [ "$status" -eq 0 ]
  [[ "$output" == *"/readyz: ready"* ]] || false
  stub curl 22 ''
  run "$MFPCTL" tunnel status
  [ "$status" -eq 1 ]
}
