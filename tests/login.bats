#!/usr/bin/env bats
# scripts/login-mfp.sh and `mfpctl login`: patchright venv -> 0600 cookie file
# -> Mason's save_session.py, with Mason stopped in between.

load helpers

setup() {
  common_setup
  LOGIN="$REPO_ROOT/scripts/login-mfp.sh"
  MFPCTL="$REPO_ROOT/bin/mfpctl"
  make_fake_mfp_mcp
  stub_security
  stub curl 0 '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"myfitnesspal","version":"1"}}}'
  stub open
  # pgrep: "Chrome is running" for N polls ($STUB_DIR/pgrep-left), then gone.
  cat >"$STUB_DIR/pgrep" <<'EOF'
#!/bin/bash
printf 'pgrep %s\n' "$*" >>"$STUB_LOG"
n="$(cat "$STUB_DIR/pgrep-left" 2>/dev/null || echo 0)"
[ "$n" -gt 0 ] || exit 1
echo $((n - 1)) >"$STUB_DIR/pgrep-left"
exit 0
EOF
  chmod +x "$STUB_DIR/pgrep"
  stub sleep
  export MFP_CHROME_APP="$TEST_HOME/Applications/Google Chrome.app"
  mkdir -p "$MFP_CHROME_APP"
  MASON_CFG="$TEST_HOME/Library/Application Support/myfitnesspal-mcp"
  mkdir -p "$MASON_CFG" "$MFP_CONFIG_DIR" "$LAUNCH_AGENTS_DIR"
  export MASON_CFG
  printf '{"cookies":{"__Secure-next-auth.session-token":"OLD-TOKEN"},"username":"jdr"}' >"$MASON_CFG/cookies.json"
  printf 'MFP_LOGIN_EMAIL=you@example.com\nMFP_USERNAME=jdr\n' >"$MFP_CONFIG_DIR/mason.env"
  chmod 600 "$MFP_CONFIG_DIR/mason.env"
  touch "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"

  # Fake login-venv Python (patchright): checks the handoff file, writes the
  # harvested cookies to it unless $STUB_DIR/login.rc says otherwise.
  export MFP_LOGIN_PY="$TEST_HOME/login-venv/bin/python"
  mkdir -p "$(dirname "$MFP_LOGIN_PY")"
  cat >"$MFP_LOGIN_PY" <<'EOF'
#!/bin/bash
if [ "$1" = -c ]; then exit "$(cat "$STUB_DIR/patchright.rc" 2>/dev/null || echo 0)"; fi
shift_args="$(basename "$1") ${2:-}"
echo "login-python ${shift_args% } mason_loaded=$([ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ] && echo yes || echo no)" >>"$STUB_LOG"
echo "COOKIE_OUT=$MFP_COOKIE_OUT MODE=$(stat -f %Lp "$MFP_COOKIE_OUT") PROFILE=$MFP_LOGIN_PROFILE EMAIL=${MFP_LOGIN_EMAIL:-} TIMEOUT=$MFP_LOGIN_TIMEOUT" >>"$STUB_LOG"
env >"$STUB_DIR/login-env"
rc="$(cat "$STUB_DIR/login.rc" 2>/dev/null || echo 0)"
if [ "$rc" = 0 ]; then
  printf '{"cookies":{"__Secure-next-auth.session-token":"NEW-TOKEN","_ga":"x"}}' >"$MFP_COOKIE_OUT"
  echo "[mfp-login] email field matched label=Email Address"
  echo "[mfp-login] Harvested 2 cookies."
fi
exit "$rc"
EOF
  chmod +x "$MFP_LOGIN_PY"

  # Fake Mason tool Python: save_session.py copies the handoff file into
  # Mason's cookies.json (what auth.save_cookies would do).
  export MFP_TOOL_PY="$TEST_HOME/tool/bin/python"
  mkdir -p "$(dirname "$MFP_TOOL_PY")"
  cat >"$MFP_TOOL_PY" <<'EOF'
#!/bin/bash
echo "tool-python $(basename "$1") COOKIE_IN=$MFP_COOKIE_IN mason_loaded=$([ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ] && echo yes || echo no)" >>"$STUB_LOG"
cp "$MFP_COOKIE_IN" "$MASON_CFG/cookies.json"
echo "[mfp-login] Saved 2 cookies; Mason profile seeded."
exit "$(cat "$STUB_DIR/save.rc" 2>/dev/null || echo 0)"
EOF
  chmod +x "$MFP_TOOL_PY"
}

hash8() { printf '%s' "$1" | shasum -a 256 | cut -c1-8; }
cookie_out() { sed -n "s/^COOKIE_OUT=\(.*\) MODE=.*/\1/p" "$STUB_LOG" | head -n 1; }

@test "missing MFP_LOGIN_EMAIL fails clearly" {
  printf 'MFP_USERNAME=jdr\n' >"$MFP_CONFIG_DIR/mason.env"
  run "$LOGIN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"MFP_LOGIN_EMAIL is not set"* ]] || false
  ! grep -q login-python "$STUB_LOG" || false
}

@test "missing Keychain item fails clearly and never reads a secret" {
  echo 44 >"$STUB_DIR/security.rc"
  run "$LOGIN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"security add-generic-password -s mfp-login -a 'you@example.com' -w"* ]] || false
  grep -qx 'security find-generic-password -s mfp-login -a you@example.com' "$STUB_LOG"
  ! grep -q -- '-w' "$STUB_LOG" || false
}

@test "success: patchright login -> 0600 handoff file -> Mason save, then auth check" {
  run "$LOGIN" --no-health --timeout 90
  [ "$status" -eq 0 ]
  [[ "$output" == *"✅ Logged in as fakeuser (token $(hash8 OLD-TOKEN) -> $(hash8 NEW-TOKEN))"* ]] || false
  out="$(cookie_out)"
  [[ "$out" == "$MFP_STATE_DIR/login-cookies."* ]] || false
  grep -q "COOKIE_OUT=$out MODE=600 PROFILE=$MFP_STATE_DIR/login-profile EMAIL=you@example.com TIMEOUT=90" "$STUB_LOG"
  grep -qx "tool-python save_session.py COOKIE_IN=$out mason_loaded=no" "$STUB_LOG"
  [ ! -e "$out" ]
  [ "$(file_mode "$MFP_STATE_DIR/login-profile")" = 700 ]
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
  [[ "$(cat "$MFP_STATE_DIR/relogin.last")" =~ ^[0-9]+\ 0$ ]] || false
  grep -q 'Mason profile seeded' "$MFP_LOG_DIR/login.log"
}

@test "Mason is stopped for the login and restarted after" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$LOGIN" --no-health
  [ "$status" -eq 0 ]
  run grep -E 'launchctl (bootout|bootstrap)|login-python|tool-python' "$STUB_LOG"
  [[ "${lines[0]}" == *"bootout gui/$(id -u)/com.jdrami.mfp-mcp" ]] || false
  [[ "${lines[1]}" == "login-python mfp_login.py mason_loaded=no" ]] || false
  [[ "${lines[2]}" == "tool-python save_session.py"*"mason_loaded=no" ]] || false
  [[ "${lines[3]}" == *"bootstrap gui/$(id -u) $LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist" ]] || false
}

@test "a failed login skips the save, restarts Mason and removes the handoff file" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  echo 5 >"$STUB_DIR/login.rc"
  run "$LOGIN" --no-health
  [ "$status" -eq 5 ]
  [[ "$output" == *"❌ Login failed (exit 5: MFP rejected the email/password)"* ]] || false
  ! grep -q tool-python "$STUB_LOG" || false
  [ -e "$STUB_DIR/loaded-com.jdrami.mfp-mcp" ]
  [ ! -e "$(cookie_out)" ]
  [[ "$(cat "$MFP_STATE_DIR/relogin.last")" == *" 5" ]] || false
}

@test "a failed Mason save is exit 7" {
  echo 2 >"$STUB_DIR/save.rc"
  run "$LOGIN" --no-health
  [ "$status" -eq 7 ]
}

@test "timeout exit points to --manual and is recorded with its exit code" {
  echo 6 >"$STUB_DIR/login.rc"
  run "$LOGIN" --no-health
  [ "$status" -eq 6 ]
  [[ "$output" == *"Turnstile/captcha not cleared; try bin/mfpctl login --manual at the screen"* ]] || false
  [[ "$(cat "$MFP_STATE_DIR/relogin.last")" == *" 6" ]] || false
}

@test "missing patchright venv is exit 8 with the install hint" {
  echo 1 >"$STUB_DIR/patchright.rc"
  run "$LOGIN" --no-health
  [ "$status" -eq 8 ]
  [[ "$output" == *"login environment missing (patchright venv"*"run scripts/install-mac.sh"* ]] || false
  ! grep -q 'login-python mfp_login' "$STUB_LOG" || false
}

@test "missing Google Chrome is exit 8" {
  rm -rf "$MFP_CHROME_APP"
  run "$LOGIN" --no-health
  [ "$status" -eq 8 ]
  [[ "$output" == *"Google Chrome"* ]] || false
}

@test "--manual opens real Chrome, waits for it to quit, then harvests; no Keychain" {
  echo 3 >"$STUB_DIR/pgrep-left"
  MFP_LOGIN_ALLOW_MANUAL=1 run "$LOGIN" --manual --no-health
  [ "$status" -eq 0 ]
  grep -qx "open -na Google Chrome --args --user-data-dir=$MFP_STATE_DIR/login-profile --no-first-run https://www.myfitnesspal.com/account/login" "$STUB_LOG"
  [ "$(grep -c '^pgrep' "$STUB_LOG")" = 4 ]
  grep -qx 'login-python mfp_login.py --manual mason_loaded=no' "$STUB_LOG"
  ! grep -q '^security' "$STUB_LOG" || false
  [[ "$output" == *"QUIT that Chrome window"* ]] || false
  [[ "$output" == *"✅ Logged in"* ]] || false
}

@test "--manual works without MFP_LOGIN_EMAIL" {
  printf 'MFP_USERNAME=jdr\n' >"$MFP_CONFIG_DIR/mason.env"
  MFP_LOGIN_ALLOW_MANUAL=1 run "$LOGIN" --manual --no-health
  [ "$status" -eq 0 ]
}

@test "--manual refuses to run unattended" {
  run bash -c "'$LOGIN' --manual --no-health </dev/null"
  [ "$status" -ne 0 ]
  [[ "$output" == *"needs you at the Mac's screen"* ]] || false
  ! grep -q '^open' "$STUB_LOG" || false
}

@test "--manual times out if Chrome is never closed" {
  echo 100000 >"$STUB_DIR/pgrep-left"
  MFP_LOGIN_ALLOW_MANUAL=1 run "$LOGIN" --manual --no-health --timeout 6
  [ "$status" -eq 6 ]
  [[ "$output" == *"Chrome was not closed within 6s"* ]] || false
  ! grep -q 'login-python mfp_login' "$STUB_LOG" || false
}

@test "the password and cookie values never appear anywhere" {
  run "$LOGIN"
  [[ "$output" != *FAKE-PASSWORD-SECRET* ]] || false
  [[ "$output" != *NEW-TOKEN* ]] || false
  ! grep -rq 'FAKE-PASSWORD-SECRET\|NEW-TOKEN' "$MFP_LOG_DIR" || false
  ! grep -q 'FAKE-PASSWORD-SECRET\|NEW-TOKEN' "$STUB_LOG" || false
  ! grep -q 'FAKE-PASSWORD-SECRET\|NEW-TOKEN' "$STUB_DIR/login-env" || false
}

@test "runs the health check afterwards with --no-heal" {
  run "$LOGIN"
  [[ "$output" == *"health: ok"* ]] || false
}

@test "--dry-run changes nothing" {
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run "$LOGIN" --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ launchctl bootout"* ]] || false
  [[ "$output" == *"mfp_login.py"*"installed Chrome via patchright"* ]] || false
  [[ "$output" == *"save_session.py"* ]] || false
  ! grep -q 'login-python\|tool-python\|launchctl boot' "$STUB_LOG" || false
  [ ! -e "$MFP_STATE_DIR/relogin.last" ]
  MFP_LOGIN_ALLOW_MANUAL=1 run "$LOGIN" --manual --dry-run
  [[ "$output" == *"+ open -na \"Google Chrome\""* ]] || false
  ! grep -q '^open' "$STUB_LOG" || false
}

@test "mfpctl login [--manual] is wired up, in help, and installed as runtime copies" {
  run "$MFPCTL" help
  [[ "$output" == *"  login [--manual] [--timeout S]"* ]] || false
  run "$MFPCTL" login --no-health
  [ "$status" -eq 0 ]
  [[ "$output" == *"✅ Logged in"* ]] || false
  MFP_LOGIN_ALLOW_MANUAL=1 run "$MFPCTL" login --manual --no-health
  [ "$status" -eq 0 ]
  rm "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-mcp.plist"
  "$MFPCTL" install
  [ -x "$MFP_STATE_DIR/bin/login-mfp.sh" ]
  [ -f "$MFP_STATE_DIR/bin/mfp_login.py" ]
  [ -f "$MFP_STATE_DIR/bin/save_session.py" ]
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

@test "relogin_allowed honours the old one-field format" {
  run allowed
  [ "$status" -eq 0 ]
  mkdir -p "$MFP_STATE_DIR"
  date +%s >"$MFP_STATE_DIR/relogin.last"
  run allowed
  [ "$status" -ne 0 ]
  echo $(($(date +%s) - 21601)) >"$MFP_STATE_DIR/relogin.last"
  run allowed
  [ "$status" -eq 0 ]
}
