#!/usr/bin/env bats
# scripts/login-mfp.sh and `mfpctl login`.

load helpers

setup() {
  common_setup
  LOGIN="$REPO_ROOT/scripts/login-mfp.sh"
  MFPCTL="$REPO_ROOT/bin/mfpctl"
  make_fake_mfp_mcp
  stub_security
  stub curl 0 '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"myfitnesspal","version":"1"}}}'
  MASON_CFG="$TEST_HOME/Library/Application Support/myfitnesspal-mcp"
  mkdir -p "$MASON_CFG" "$MFP_CONFIG_DIR" "$LAUNCH_AGENTS_DIR"
  export MASON_CFG
  printf '{"cookies":{"__Secure-next-auth.session-token":"OLD-TOKEN"},"username":"jdr"}' >"$MASON_CFG/cookies.json"
  printf 'MFP_LOGIN_EMAIL=you@example.com\nMFP_USERNAME=jdr\n' >"$MFP_CONFIG_DIR/mason.env"
  chmod 600 "$MFP_CONFIG_DIR/mason.env"
  touch "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
  # Fake tool Python: records the env it got (names, plus the email) and
  # whether Mason was loaded; writes a new session unless login.rc says no.
  export MFP_TOOL_PY="$TEST_HOME/tool/bin/python"
  mkdir -p "$(dirname "$MFP_TOOL_PY")"
  cat >"$MFP_TOOL_PY" <<'EOF'
#!/bin/bash
echo "tool-python $(basename "$1") mason_loaded=$([ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ] && echo yes || echo no)" >>"$STUB_LOG"
env | sed 's/=.*//' | sort >"$STUB_DIR/login-env-names"
env >"$STUB_DIR/login-env"
echo "LOGIN_EMAIL=$MFP_LOGIN_EMAIL TIMEOUT=$MFP_LOGIN_TIMEOUT" >>"$STUB_LOG"
rc="$(cat "$STUB_DIR/login.rc" 2>/dev/null || echo 0)"
if [ "$rc" = 0 ]; then
  printf '{"cookies":{"__Secure-next-auth.session-token":"NEW-TOKEN","_ga":"x"}}' >"$MASON_CFG/cookies.json"
  echo "[mfp-login] email field matched label=Email Address"
  echo "[mfp-login] Saved 2 cookies."
fi
exit "$rc"
EOF
  chmod +x "$MFP_TOOL_PY"
}

hash8() { printf '%s' "$1" | shasum -a 256 | cut -c1-8; }

@test "missing MFP_LOGIN_EMAIL fails clearly" {
  printf 'MFP_USERNAME=jdr\n' >"$MFP_CONFIG_DIR/mason.env"
  run "$LOGIN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"MFP_LOGIN_EMAIL is not set"* ]] || false
  ! grep -q tool-python "$STUB_LOG" || false
}

@test "missing Keychain item fails clearly and never reads a secret" {
  echo 44 >"$STUB_DIR/security.rc"
  run "$LOGIN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"security add-generic-password -s mfp-login -a 'you@example.com' -w"* ]] || false
  grep -qx 'security find-generic-password -s mfp-login -a you@example.com' "$STUB_LOG"
  ! grep -q -- '-w' "$STUB_LOG" || false
  ! grep -q tool-python "$STUB_LOG" || false
}

@test "success logs in, checks auth and records the attempt" {
  run "$LOGIN" --no-health --timeout 90
  [ "$status" -eq 0 ]
  [[ "$output" == *"✅ Logged in as fakeuser (token $(hash8 OLD-TOKEN) -> $(hash8 NEW-TOKEN))"* ]] || false
  grep -qx 'tool-python mfp_login.py mason_loaded=no' "$STUB_LOG"
  grep -qx 'LOGIN_EMAIL=you@example.com TIMEOUT=90' "$STUB_LOG"
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
  [ -s "$MFP_STATE_DIR/relogin.last" ]
  grep -q 'Saved 2 cookies' "$MFP_LOG_DIR/login.log"
}

@test "Mason is stopped for the login and restarted after" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$LOGIN" --no-health
  [ "$status" -eq 0 ]
  run grep -E 'launchctl (bootout|bootstrap)|tool-python' "$STUB_LOG"
  [[ "${lines[0]}" == *"bootout gui/$(id -u)/com.jdrami.mfp-mcp" ]] || false
  [[ "${lines[1]}" == "tool-python mfp_login.py mason_loaded=no" ]] || false
  [[ "${lines[2]}" == *"bootstrap gui/$(id -u) $LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist" ]] || false
}

@test "a failed login still restarts Mason and records the attempt" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  echo 5 >"$STUB_DIR/login.rc"
  run "$LOGIN" --no-health
  [ "$status" -eq 5 ]
  [[ "$output" == *"❌ Login failed (exit 5: MFP rejected the email/password)"* ]] || false
  [ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ]
  [ -s "$MFP_STATE_DIR/relogin.last" ]
  ! grep -qx 'mfp-mcp auth --check' "$STUB_LOG" || false
}

@test "timeout exit is explained as Turnstile and recorded with its exit code" {
  echo 6 >"$STUB_DIR/login.rc"
  run "$LOGIN" --no-health
  [ "$status" -eq 6 ]
  [[ "$output" == *"timed out, Turnstile/captcha needs a click"* ]] || false
  [[ "$(cat "$MFP_STATE_DIR/relogin.last")" == *" 6" ]] || false
}

@test "a successful login records exit 0 in relogin.last" {
  run "$LOGIN" --no-health
  [ "$status" -eq 0 ]
  [[ "$(cat "$MFP_STATE_DIR/relogin.last")" =~ ^[0-9]+\ 0$ ]] || false
}

@test "the password never appears anywhere" {
  run "$LOGIN"
  [[ "$output" != *FAKE-PASSWORD-SECRET* ]] || false
  ! grep -rq FAKE-PASSWORD-SECRET "$MFP_LOG_DIR" || false
  ! grep -q FAKE-PASSWORD-SECRET "$STUB_LOG" || false
  ! grep -q FAKE-PASSWORD-SECRET "$STUB_DIR/login-env" || false
}

@test "runs the health check afterwards with --no-heal" {
  run "$LOGIN"
  [[ "$output" == *"health: ok"* ]] || false
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
}

@test "--dry-run changes nothing" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$LOGIN" --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ launchctl bootout"* ]] || false
  [[ "$output" == *"mfp_login.py"* ]] || false
  ! grep -q 'tool-python\|launchctl boot' "$STUB_LOG" || false
  [ ! -e "$MFP_STATE_DIR/relogin.last" ]
}

@test "mfpctl login is wired up, in help, and installed as a runtime copy" {
  run "$MFPCTL" help
  [[ "$output" == *"  login [--timeout S]"* ]] || false
  run "$MFPCTL" login --no-health
  [ "$status" -eq 0 ]
  [[ "$output" == *"✅ Logged in"* ]] || false
  rm "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
  "$MFPCTL" install
  [ -x "$MFP_STATE_DIR/bin/login-mfp.sh" ]
  [ -f "$MFP_STATE_DIR/bin/mfp_login.py" ]
}

allowed() { bash -c ". '$REPO_ROOT/scripts/lib/common.sh'; mfp_init_paths; relogin_allowed"; }

@test "relogin retries after 1 h when the last attempt hit Turnstile (exit 6)" {
  mkdir -p "$MFP_STATE_DIR"
  echo "$(($(date +%s) - 3500)) 6" >"$MFP_STATE_DIR/relogin.last"
  run allowed
  [ "$status" -ne 0 ]
  echo "$(($(date +%s) - 3601)) 6" >"$MFP_STATE_DIR/relogin.last"
  run allowed
  [ "$status" -eq 0 ]
}

@test "relogin still waits 6 h after a credentials rejection (exit 5)" {
  mkdir -p "$MFP_STATE_DIR"
  echo "$(($(date +%s) - 3601)) 5" >"$MFP_STATE_DIR/relogin.last"
  run allowed
  [ "$status" -ne 0 ]
  echo "$(($(date +%s) - 21601)) 5" >"$MFP_STATE_DIR/relogin.last"
  run allowed
  [ "$status" -eq 0 ]
}

@test "relogin_allowed honours the 6 h window" {
  run bash -c ". '$REPO_ROOT/scripts/lib/common.sh'; mfp_init_paths; relogin_allowed"
  [ "$status" -eq 0 ]
  mkdir -p "$MFP_STATE_DIR"
  date +%s >"$MFP_STATE_DIR/relogin.last"
  run bash -c ". '$REPO_ROOT/scripts/lib/common.sh'; mfp_init_paths; relogin_allowed"
  [ "$status" -ne 0 ]
  echo $(($(date +%s) - 21601)) >"$MFP_STATE_DIR/relogin.last"
  run bash -c ". '$REPO_ROOT/scripts/lib/common.sh'; mfp_init_paths; relogin_allowed"
  [ "$status" -eq 0 ]
}
