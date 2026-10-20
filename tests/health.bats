#!/usr/bin/env bats
# scripts/health-check.sh: state-change alerting with stubbed curl + mfp-mcp.

load helpers

TOKEN="123456789:AAH-secret_TOKEN-value"

setup() {
  common_setup
  HEALTH="$REPO_ROOT/scripts/health-check.sh"
  make_fake_mfp_mcp
  STATE="$MFP_STATE_DIR/health.state"
  mkdir -p "$MFP_STATE_DIR"
  SENT="$BATS_TEST_TMPDIR/telegram-sent"
  : >"$SENT"
  export SENT
  write_curl_stub
  healthy
  # Healing steps are stubs: each records its call and can "fix" auth.
  export MFP_HEAL_MCP_WAIT=0
  export MFP_REFRESH_SCRIPT="$STUB_DIR/refresh-stub.sh" MFP_LOGIN_SCRIPT="$STUB_DIR/login-stub.sh"
  for step in refresh login; do
    cat >"$STUB_DIR/$step-stub.sh" <<EOF
#!/bin/bash
echo "heal-$step \$*" >>"\$STUB_LOG"
[ -e "\$STUB_DIR/$step-fixes" ] && echo 0 >"\$STUB_DIR/auth.rc"
exit 0
EOF
  done
  stub_security
}

password_login_configured() {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_LOGIN_EMAIL=you@example.com\n' >"$MFP_CONFIG_DIR/mason.env"
  chmod 600 "$MFP_CONFIG_DIR/mason.env"
}

# curl stub: routes on the URL / -K flag.
#   MCP probe   -> $STUB_DIR/mcp.rc (0 = valid initialize response)
#   /readyz     -> $STUB_DIR/readyz.rc
#   Telegram    -> $STUB_DIR/telegram.rc; stdin config + --data-urlencode
#                  args are appended to $SENT (one "SEND" marker per call).
write_curl_stub() {
  cat >"$STUB_DIR/curl" <<'EOF'
#!/bin/bash
printf 'curl' >>"$STUB_LOG"; for a in "$@"; do printf ' %s' "$a" >>"$STUB_LOG"; done; printf '\n' >>"$STUB_LOG"
rc_of() { cat "$STUB_DIR/$1.rc" 2>/dev/null || echo 0; }
case " $* " in
  *" -K - "*)
    { echo SEND; cat; for a in "$@"; do echo "ARG $a"; done; } >>"$SENT"
    exit "$(rc_of telegram)" ;;
  *"/readyz"*) exit "$(rc_of readyz)" ;;
  *"/mcp"*)
    rc="$(rc_of mcp)"
    [ "$rc" = 0 ] && echo '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"myfitnesspal","version":"1"}}}'
    exit "$rc" ;;
esac
exit 0
EOF
  chmod +x "$STUB_DIR/curl"
}

healthy() { echo 0 >"$STUB_DIR/mcp.rc"; echo 0 >"$STUB_DIR/auth.rc"; echo 0 >"$STUB_DIR/readyz.rc"; }
alerts_configured() {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=42\n' "$TOKEN" >"$MFP_CONFIG_DIR/alert.env"
  chmod 600 "$MFP_CONFIG_DIR/alert.env"
}
sends() { grep -c '^SEND$' "$SENT" || true; }

@test "ok -> ok sends nothing and records ok" {
  alerts_configured
  run "$HEALTH"
  [ "$status" -eq 0 ]
  [ "$(sends)" = 0 ]
  [ "$(cat "$STATE")" = ok ]
  run "$HEALTH"
  [ "$status" -eq 0 ]
  [ "$(sends)" = 0 ]
}

@test "ok -> auth failure sends exactly one red alert with a hint" {
  alerts_configured
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$status" -eq 1 ]
  [ "$(sends)" = 1 ]
  grep -q '^ARG text=🔴 MFP Mason unhealthy' "$SENT"
  grep -q 'auth: Status: rejected (MyfitnesspalLoginError: expired)' "$SENT"
  grep -q 'mfpctl login' "$SENT"
  grep -q '^ARG chat_id=42$' "$SENT"
  [ "$(cat "$STATE")" = fail:auth ]
}

@test "fail -> same fail sends nothing" {
  alerts_configured
  echo fail:auth >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$status" -eq 1 ]
  [ "$(sends)" = 0 ]
  [[ "$output" == *"no alert sent"* ]] || false
}

@test "fail -> different fail alerts again" {
  alerts_configured
  echo fail:auth >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  echo 7 >"$STUB_DIR/mcp.rc"
  run "$HEALTH"
  [ "$(sends)" = 1 ]
  [ "$(cat "$STATE")" = fail:mcp,auth ]
  grep -q 'mfpctl restart' "$SENT"
}

@test "fail -> ok sends one green recovery" {
  alerts_configured
  echo fail:mcp >"$STATE"
  run "$HEALTH"
  [ "$status" -eq 0 ]
  [ "$(sends)" = 1 ]
  grep -q '^ARG text=🟢 MFP Mason recovered' "$SENT"
  [ "$(cat "$STATE")" = ok ]
}

@test "first run that is already failing alerts" {
  alerts_configured
  echo 7 >"$STUB_DIR/mcp.rc"
  run "$HEALTH"
  [ "$(sends)" = 1 ]
}

@test "a failed send leaves the state unchanged so it retries" {
  alerts_configured
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  echo 22 >"$STUB_DIR/telegram.rc"
  run "$HEALTH"
  [ "$(sends)" = 1 ]
  [ "$(cat "$STATE")" = ok ]
  [[ "$output" == *"next run retries"* ]] || false
}

@test "the bot token never appears in argv, output or logs" {
  alerts_configured
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$(sends)" = 1 ]
  # It does reach curl, but only through the stdin config.
  grep -q "^url = \"https://api.telegram.org/bot$TOKEN/sendMessage\"$" "$SENT"
  ! grep -qF "$TOKEN" "$STUB_LOG" || false
  ! grep -F "ARG" "$SENT" | grep -qF "$TOKEN" || false
  [[ "$output" != *"$TOKEN"* ]] || false
  ! grep -rqF "$TOKEN" "$MFP_LOG_DIR" || false
}

@test "missing alert.env warns, exits 0 when healthy, and keeps retrying" {
  run "$HEALTH"
  [ "$status" -eq 0 ]
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$status" -eq 1 ]
  [[ "$output" == *"alerts are not configured"* ]] || false
  [ "$(cat "$STATE")" = ok ]
}

@test "malformed token is refused without sending" {
  alerts_configured
  printf 'TELEGRAM_BOT_TOKEN="x"; rm -rf /\nTELEGRAM_CHAT_ID=42\n' >"$MFP_CONFIG_DIR/alert.env"
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$(sends)" = 0 ]
  [[ "$output" == *"missing or malformed"* ]] || false
}

@test "loose alert.env permissions produce a warning" {
  alerts_configured
  chmod 644 "$MFP_CONFIG_DIR/alert.env"
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [[ "$output" == *"has mode 644"* ]] || false
}

@test "tunnel check runs only when the tunnel plist exists" {
  alerts_configured
  echo 7 >"$STUB_DIR/readyz.rc"
  run "$HEALTH"
  [ "$status" -eq 0 ]
  ! grep -q readyz "$STUB_LOG" || false
  mkdir -p "$LAUNCH_AGENTS_DIR" && touch "$LAUNCH_AGENTS_DIR/com.jdrami.mfp-tunnel.plist"
  run "$HEALTH"
  [ "$status" -eq 1 ]
  grep -q 'http://127.0.0.1:8080/readyz' "$STUB_LOG"
  [ "$(cat "$STATE")" = fail:tunnel ]
}

@test "--dry-run --simulate-fail auth prints the alert and changes nothing" {
  alerts_configured
  run "$HEALTH" --dry-run --simulate-fail auth
  [ "$status" -eq 1 ]
  [[ "$output" == *"would send this Telegram message"* ]] || false
  [[ "$output" == *"🔴 MFP Mason unhealthy"* ]] || false
  [[ "$output" == *"auth: simulated failure"* ]] || false
  [ "$(sends)" = 0 ]
  [ ! -e "$STATE" ]
  [ ! -e "$MFP_LOG_DIR/health.log" ]
}

@test "mfp-mcp auth --check is the only Mason command run" {
  run "$HEALTH"
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
  [ "$(grep -c '^mfp-mcp' "$STUB_LOG")" = 1 ]
}

@test "health plist is lint-clean, every 6 h incl. 09:00, with no KeepAlive" {
  "$REPO_ROOT/bin/mfpctl" install
  p="$LAUNCH_AGENTS_DIR/com.jdrami.mfp-health.plist"
  plutil -lint "$p"
  [ "$(plutil -extract StartCalendarInterval raw -o - "$p")" = 4 ]
  hours=""
  for i in 0 1 2 3; do
    hours="$hours $(plutil -extract "StartCalendarInterval.$i.Hour" raw -o - "$p")"
    [ "$(plutil -extract "StartCalendarInterval.$i.Minute" raw -o - "$p")" = 0 ]
  done
  [ "$hours" = " 3 9 15 21" ]
  [ "$(plutil -extract RunAtLoad raw -o - "$p")" = true ]
  run plutil -extract KeepAlive raw -o - "$p"
  [ "$status" -ne 0 ]
  script="$(plutil -extract ProgramArguments.1 raw -o - "$p")"
  [ "$script" = "$MFP_STATE_DIR/bin/health-check.sh" ]
  [ -x "$script" ]
  [ -f "$MFP_STATE_DIR/bin/lib/common.sh" ]
  grep -qx "launchctl bootstrap gui/$(id -u) $p" "$STUB_LOG"
}

@test "the installed copy of the health check runs on its own" {
  "$REPO_ROOT/bin/mfpctl" install
  run /bin/bash "$MFP_STATE_DIR/bin/health-check.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == "health: ok"* ]] || false
}

@test "mfpctl health forwards flags to the script" {
  run "$REPO_ROOT/bin/mfpctl" health --simulate-fail mcp --dry-run
  [ "$status" -eq 1 ]
  [[ "$output" == *"mcp: simulated failure"* ]] || false
  [[ "$output" == *"would send"* ]] || false
}

@test "heal: refresh fixes auth -> no alert, login not tried" {
  alerts_configured
  password_login_configured
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  touch "$STUB_DIR/refresh-fixes"
  run "$HEALTH"
  [ "$status" -eq 0 ]
  [ "$(sends)" = 0 ]
  grep -qx 'heal-refresh --no-health' "$STUB_LOG"
  ! grep -q heal-login "$STUB_LOG" || false
  grep -q 'healed by refresh' "$MFP_LOG_DIR/health.log"
  [ "$(cat "$STATE")" = ok ]
}

@test "heal: refresh fails, password re-login fixes auth -> no alert" {
  alerts_configured
  password_login_configured
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  touch "$STUB_DIR/login-fixes"
  run "$HEALTH"
  [ "$status" -eq 0 ]
  [ "$(sends)" = 0 ]
  grep -qx 'heal-login --no-health' "$STUB_LOG"
  grep -q 'healed by re-login' "$MFP_LOG_DIR/health.log"
}

@test "heal: both fail -> one red alert naming both attempts" {
  alerts_configured
  password_login_configured
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$status" -eq 1 ]
  [ "$(sends)" = 1 ]
  grep -q 'refresh failed; password re-login failed' "$SENT"
  grep -q 'mfpctl login' "$SENT"
}

@test "heal: a recent re-login is rate-limited" {
  alerts_configured
  password_login_configured
  date +%s >"$MFP_STATE_DIR/relogin.last"
  echo ok >"$STATE"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  [ "$(sends)" = 1 ]
  grep -q heal-refresh "$STUB_LOG"
  ! grep -q heal-login "$STUB_LOG" || false
  grep -q 're-login skipped (rate limit, next after [0-9][0-9]:[0-9][0-9])' "$SENT"
}

@test "heal: no MFP_LOGIN_EMAIL means no re-login" {
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  grep -q heal-refresh "$STUB_LOG"
  ! grep -q heal-login "$STUB_LOG" || false
  [[ "$output" == *"no password login configured"* ]] || false
}

@test "heal: no Keychain item means no re-login" {
  password_login_configured
  echo 44 >"$STUB_DIR/security.rc"
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH"
  ! grep -q heal-login "$STUB_LOG" || false
  [[ "$output" == *"no Keychain password for mfp-login"* ]] || false
}

@test "heal: --no-heal, --dry-run and --simulate-fail auth never heal" {
  password_login_configured
  echo 1 >"$STUB_DIR/auth.rc"
  run "$HEALTH" --no-heal
  run "$HEALTH" --dry-run
  echo 0 >"$STUB_DIR/auth.rc"
  run "$HEALTH" --simulate-fail auth --dry-run
  run "$HEALTH" --simulate-fail auth
  ! grep -q 'heal-' "$STUB_LOG" || false
}

@test "refresh-session.sh runs the health check with --no-heal" {
  grep -q 'health-check.sh" --wait 120 --no-heal' "$REPO_ROOT/scripts/refresh-session.sh"
  grep -q 'health-check.sh" --wait 120 --no-heal' "$REPO_ROOT/scripts/login-mfp.sh"
}
