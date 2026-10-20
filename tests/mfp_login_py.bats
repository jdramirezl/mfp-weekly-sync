#!/usr/bin/env bats
# scripts/mfp_login.py against a fake patchright (wrapping a fake Playwright).

load helpers

PASSWORD="FAKE-PASSWORD-SECRET"

setup() {
  [ -x /usr/bin/python3 ] || skip "needs /usr/bin/python3"
  common_setup
  LOGIN_PY="$REPO_ROOT/scripts/mfp_login.py"
  export PYTHONPATH="$REPO_ROOT/tests/fakepy"
  export PYTHONDONTWRITEBYTECODE=1
  export FAKE_PW_LOG="$BATS_TEST_TMPDIR/pw.log"
  : >"$FAKE_PW_LOG"
  export MFP_LOGIN_EMAIL="you@example.com" MFP_LOG_DIR="$BATS_TEST_TMPDIR/logs"
  export MFP_LOGIN_PROFILE="$MFP_STATE_DIR/login-profile"
  export MFP_COOKIE_OUT="$MFP_STATE_DIR/login-cookies.test"
  OUT="$MFP_COOKIE_OUT"
  mkdir -p "$MFP_LOG_DIR" "$MFP_STATE_DIR"
  : >"$OUT"
  stub_security
}

py() { /usr/bin/python3 "$LOGIN_PY" "$@"; }

@test "fills email and password via the labelled fields, clicks Log in, harvests all MFP cookies" {
  run py
  [ "$status" -eq 0 ]
  [[ "$output" == *"email field matched label=Email Address"* ]] || false
  [[ "$output" == *"password field matched label=Password"* ]] || false
  [[ "$output" == *"submit button matched role-button"* ]] || false
  [[ "$output" == *"Harvested 3 cookies."* ]] || false
  grep -qx "type label=Email Address you@example.com" "$FAKE_PW_LOG"
  grep -qx "type label=Password $PASSWORD" "$FAKE_PW_LOG"
  grep -qx "launch channel=chrome headless=False no_viewport=True" "$FAKE_PW_LOG"
  grep -qx "launch $MFP_LOGIN_PROFILE headless=False" "$FAKE_PW_LOG"
  ! grep -q bad-launch-kwarg "$FAKE_PW_LOG" || false
  grep -q '"__Secure-next-auth.session-token": "SESSION-VALUE"' "$OUT"
  grep -q '"__cf_bm"' "$OUT"
  ! grep -q '"other"' "$OUT" || false
  [ "$(file_mode "$OUT")" = 600 ]
  [ "$(file_mode "$MFP_LOGIN_PROFILE")" = 700 ]
  grep -qx close "$FAKE_PW_LOG"
}

@test "the password is never printed" {
  run py
  [ "$status" -eq 0 ]
  [[ "$output" != *"$PASSWORD"* ]] || false
  ! grep -q "$PASSWORD" "$STUB_LOG" || false
}

@test "falls back to CSS selectors when labels are missing" {
  FAKE_PW_LABELS="" FAKE_PW_ROLE=0 run py
  [ "$status" -eq 0 ]
  [[ "$output" == *"email field matched css=input[type=email]"* ]] || false
  [[ "$output" == *"password field matched css=input[type=password]"* ]] || false
  [[ "$output" == *"submit button matched css=button[type=submit]"* ]] || false
}

@test "never clicks an SSO button" {
  run py
  ! grep -i '^click.*\(google\|facebook\|apple\)' "$FAKE_PW_LOG" || false
  [ "$(grep -c '^click' "$FAKE_PW_LOG")" = 1 ]
}

@test "an already-logged-in profile skips the form" {
  FAKE_PW_MODE=already run py
  [ "$status" -eq 0 ]
  [[ "$output" == *"already logged in"* ]] || false
  ! grep -q '^\(fill\|type\)' "$FAKE_PW_LOG" || false
  [ -s "$OUT" ]
}

@test "a challenge prints the wait message once, then succeeds" {
  FAKE_PW_MODE=challenge run py
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -c 'Waiting for you to finish the challenge')" = 1 ]
  [[ "$output" == *"Harvested"* ]] || false
}

@test "rejected credentials exit 5 with a screenshot" {
  FAKE_PW_MODE=rejected run py
  [ "$status" -eq 5 ]
  [[ "$output" == *"error banner: Incorrect email or password"* ]] || false
  [[ "$output" == *"MFP rejected the email/password"* ]] || false
  [ -e "$MFP_LOG_DIR/login-failure.png" ]
  [ ! -s "$OUT" ]
}

@test "no session within the timeout exits 6" {
  FAKE_PW_MODE=timeout MFP_LOGIN_TIMEOUT=1 run py
  [ "$status" -eq 6 ]
  [[ "$output" == *"no MFP session after 1s (last state: still on login page)"* ]] || false
}

@test "a missing Keychain item exits 4 without launching a browser" {
  echo 44 >"$STUB_DIR/security.rc"
  run py
  [ "$status" -eq 4 ]
  [[ "$output" == *"security add-generic-password -s mfp-login -a <email> -w"* ]] || false
  ! grep -q '^launch' "$FAKE_PW_LOG" || false
}

@test "reads the password from the configured Keychain service" {
  MFP_LOGIN_KEYCHAIN_SERVICE=other-svc run py
  grep -qx 'security find-generic-password -s other-svc -a you@example.com -w' "$STUB_LOG"
}

@test "Spanish credentials error also counts as rejected" {
  FAKE_PW_MODE=spanish_rejected run py
  [ "$status" -eq 5 ]
  [[ "$output" == *"error banner: Contraseña incorrecta"* ]] || false
}

@test "turnstile: waits for the token before clicking Log in" {
  FAKE_PW_MODE=turnstile run py
  [ "$status" -eq 0 ]
  ! grep -q '^click-disabled' "$FAKE_PW_LOG" || false
  [ "$(grep -c '^click ' "$FAKE_PW_LOG")" = 1 ]
  [[ "$output" == *"turnstile pending"*"turnstile solved"* ]] || false
  [[ "$output" == *"button disabled"*"button enabled"* ]] || false
  [[ "$output" != *TS-TOKEN-VALUE* ]] || false
  [[ "$output" == *"Harvested"* ]] || false
}

@test "turnstile widget with role=alert 'Verifying…' is NOT a rejection (setup-day bug)" {
  FAKE_PW_MODE=turnstile_widget_alert run py
  [ "$status" -eq 0 ]
  [[ "$output" != *"rejected"* ]] || false
  [[ "$output" != *"error banner"* ]] || false
  ! grep -q '^click-disabled' "$FAKE_PW_LOG" || false
}

@test "interactive turnstile asks for a click once, then succeeds" {
  FAKE_PW_MODE=turnstile_interactive run py
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -c 'haz clic en la casilla de Cloudflare')" = 1 ]
  ! grep -q '^click-disabled' "$FAKE_PW_LOG" || false
}

@test "turnstile that never clears times out (exit 6) without clicking" {
  FAKE_PW_MODE=turnstile_stuck MFP_TURNSTILE_TIMEOUT=1 MFP_LOGIN_TIMEOUT=2 run py
  [ "$status" -eq 6 ]
  [[ "$output" == *"turnstile pending at timeout"* ]] || false
  [[ "$output" == *"last state: turnstile pending"* ]] || false
  ! grep -q '^click' "$FAKE_PW_LOG" || false
  [ -e "$MFP_LOG_DIR/login-failure.png" ]
}

@test "turnstile reappearing after submit gets one more submit, then succeeds" {
  FAKE_PW_MODE=post_submit_turnstile run py
  [ "$status" -eq 0 ]
  [ "$(grep -c '^click ' "$FAKE_PW_LOG")" = 2 ]
  ! grep -q '^click-disabled' "$FAKE_PW_LOG" || false
  [ "$(printf '%s\n' "$output" | grep -c 'haz clic en la casilla de Cloudflare')" = 1 ]
}

@test "the password is typed, never printed, even through Turnstile" {
  FAKE_PW_MODE=turnstile run py
  grep -qx "type label=Password $PASSWORD" "$FAKE_PW_LOG"
  [[ "$output" != *"$PASSWORD"* ]] || false
}

@test "turnstile unsolved but the button enabled is never clicked (09:00 race)" {
  FAKE_PW_MODE=turnstile_stuck_enabled MFP_TURNSTILE_TIMEOUT=1 MFP_LOGIN_TIMEOUT=2 run py
  [ "$status" -eq 6 ]
  ! grep -q '^click' "$FAKE_PW_LOG" || false
  [[ "$output" == *"turnstile pending at timeout"* ]] || false
  [ ! -s "$OUT" ]
}

@test "turnstile error is logged once; a retried box then succeeds" {
  FAKE_PW_MODE=turnstile_failed FAKE_TS_RETRY=1 run py
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -c 'turnstile failed: Error: Verification failed')" = 1 ]
  ! grep -q '^click-disabled' "$FAKE_PW_LOG" || false
  [[ "$output" == *"Harvested"* ]] || false
}

@test "turnstile that keeps failing times out with last state turnstile failed" {
  FAKE_PW_MODE=turnstile_failed FAKE_TS_FAIL_POLLS=100000 MFP_TURNSTILE_TIMEOUT=1 MFP_LOGIN_TIMEOUT=2 run py
  [ "$status" -eq 6 ]
  [[ "$output" == *"last state: turnstile failed"* ]] || false
  ! grep -q '^click' "$FAKE_PW_LOG" || false
}

@test "a browser TimeoutError is exit 6 (timeout), not 7" {
  FAKE_PW_MODE=goto_timeout run py
  [ "$status" -eq 6 ]
  [[ "$output" == *"browser timed out"* ]] || false
}

@test "--manual harvests the profile's session without Keychain or typing" {
  FAKE_PW_MODE=manual run py --manual
  [ "$status" -eq 0 ]
  ! grep -q '^security' "$STUB_LOG" || false
  ! grep -q '^\(fill\|type\|click\)' "$FAKE_PW_LOG" || false
  grep -qx "launch channel=chrome headless=False no_viewport=True" "$FAKE_PW_LOG"
  grep -q 'SESSION-VALUE' "$OUT"
  [[ "$output" == *"Harvested 3 cookies."* ]] || false
  [[ "$output" != *SESSION-VALUE* ]] || false
}

@test "--manual with no session in the profile exits 6 with a hint" {
  FAKE_PW_MODE=manual_empty MFP_MANUAL_WAIT=1 run py --manual
  [ "$status" -eq 6 ]
  [[ "$output" == *"no MFP session in the login profile; did you log in before closing Chrome?"* ]] || false
  [ ! -s "$OUT" ]
}

@test "MFP_LOGIN_PROFILE and MFP_COOKIE_OUT are required" {
  unset MFP_COOKIE_OUT
  run py
  [ "$status" -eq 7 ]
  [[ "$output" == *"MFP_COOKIE_OUT"* ]] || false
}

@test "cookie and token values never reach output" {
  FAKE_PW_MODE=turnstile run py
  [ "$status" -eq 0 ]
  [[ "$output" != *SESSION-VALUE* ]] || false
  [[ "$output" != *TS-TOKEN-VALUE* ]] || false
}
