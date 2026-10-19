#!/usr/bin/env bats
# bin/mfp-auth and `mfpctl auth`: piped cookie re-authentication.

load helpers

COOKIE="eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..SECRETCOOKIEVALUE.xyz"

setup() {
  common_setup
  AUTH="$REPO_ROOT/bin/mfp-auth"
  make_fake_mfp_mcp
}

@test "cookie piped without a trailing newline reaches mfp-mcp auth intact" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "$COOKIE" ]
  [[ "$output" == *"Input received (${#COOKIE} characters). Validating…"* ]]
  [[ "$output" == *"✅ Connected as fakeuser; auto-refresh ready"* ]]
  grep -qx 'mfp-mcp auth' "$STUB_LOG"
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
}

@test "the cookie never appears in output, argv or the environment" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [[ "$output" != *"SECRETCOOKIEVALUE"* ]]
  ! grep -q SECRETCOOKIEVALUE "$STUB_LOG"
  ! grep -q SECRETCOOKIEVALUE "$STUB_DIR/auth-env"
}

@test "a Cookie: prefix, CRLF and surrounding spaces are stripped" {
  run bash -c "printf 'Cookie:  %s \r\n' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "$COOKIE" ]
  [[ "$output" == *"Input received (${#COOKIE} characters)"* ]]
}

@test "empty input fails with the pbpaste hint" {
  run bash -c "printf '  \n' | '$AUTH'"
  [ "$status" -ne 0 ]
  [[ "$output" == *"pbpaste | bin/mfp-auth"* ]]
  ! grep -q '^mfp-mcp' "$STUB_LOG"
}

@test "a terminal on stdin is refused instead of hanging" {
  MFP_AUTH_ASSUME_TTY=1 run bash -c "printf x | '$AUTH'"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no cookie on stdin"* ]]
  [[ "$output" == *"pbpaste | bin/mfp-auth"* ]]
}

@test "--username reaches Mason as MFP_USERNAME" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdramirezl"
  [ "$(cat "$STUB_DIR/auth-username")" = jdramirezl ]
}

@test "username falls back to mason.env" {
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_USERNAME=fromfile\n' >"$MFP_CONFIG_DIR/mason.env"
  chmod 600 "$MFP_CONFIG_DIR/mason.env"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH'"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-username")" = fromfile ]
}

@test "missing username warns but still tries" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"need MFP_USERNAME"* ]]
}

@test "a rejected login exits 1 with the SSO and 403 hints" {
  echo 1 >"$STUB_DIR/auth-login.rc"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH'"
  [ "$status" -eq 1 ]
  [[ "$output" == *"❌ MyFitnessPal rejected the cookie"* ]]
  [[ "$output" == *"Those cookies didn't authenticate"* ]]
  [[ "$output" == *"MFP_USERNAME"* ]]
  [[ "$output" == *"chrome124"* ]]
  ! grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
}

@test "a failing post-auth check exits 1" {
  echo 1 >"$STUB_DIR/auth.rc"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 1 ]
  [[ "$output" == *"❌ Saved, but the check failed: rejected"* ]]
}

@test "success restarts Mason only when it is loaded" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  ! grep -q 'launchctl kickstart' "$STUB_LOG"
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  grep -qx "launchctl kickstart -k gui/$(id -u)/com.jdrami.mfp-mcp" "$STUB_LOG"
  : >"$STUB_LOG"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr --no-restart"
  ! grep -q 'launchctl kickstart' "$STUB_LOG"
}

@test "--dry-run validates input but runs nothing" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --dry-run --username jdr"
  [ "$status" -eq 0 ]
  [[ "$output" == *"<cookie on stdin> | MFP_USERNAME=jdr"* ]]
  [[ "$output" != *"SECRETCOOKIEVALUE"* ]]
  ! grep -q '^mfp-mcp' "$STUB_LOG"
}

@test "mfpctl auth pipes through end to end" {
  run bash -c "printf '%s' '$COOKIE' | '$REPO_ROOT/bin/mfpctl' auth --username jdr"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "$COOKIE" ]
  [[ "$output" == *"✅ Connected as"* ]]
}
