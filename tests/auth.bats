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
  [[ "$output" == *"Input received (${#COOKIE} characters, 1 cookies). Validating…"* ]] || false
  [[ "$output" == *"✅ Connected as fakeuser; auto-refresh ready"* ]] || false
  grep -qx 'mfp-mcp auth' "$STUB_LOG"
  grep -qx 'mfp-mcp auth --check' "$STUB_LOG"
}

@test "the cookie never appears in output, argv or the environment" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [[ "$output" != *"SECRETCOOKIEVALUE"* ]] || false
  ! grep -q SECRETCOOKIEVALUE "$STUB_LOG" || false
  ! grep -q SECRETCOOKIEVALUE "$STUB_DIR/auth-env" || false
}

@test "a Cookie: prefix, CRLF and surrounding spaces are stripped" {
  run bash -c "printf 'Cookie:  %s \r\n' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "$COOKIE" ]
  [[ "$output" == *"Input received (${#COOKIE} characters, 1 cookies)"* ]] || false
}

@test "empty input fails with the pbpaste hint" {
  run bash -c "printf '  \n' | '$AUTH'"
  [ "$status" -ne 0 ]
  [[ "$output" == *"pbpaste | bin/mfp-auth"* ]] || false
  ! grep -q '^mfp-mcp' "$STUB_LOG" || false
}

@test "a terminal on stdin is refused instead of hanging" {
  MFP_AUTH_ASSUME_TTY=1 run bash -c "printf x | '$AUTH'"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no cookie on stdin"* ]] || false
  [[ "$output" == *"pbpaste | bin/mfp-auth"* ]] || false
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
  [[ "$output" == *"need MFP_USERNAME"* ]] || false
}

@test "a rejected login exits 1 with the SSO and 403 hints" {
  echo 1 >"$STUB_DIR/auth-login.rc"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH'"
  [ "$status" -eq 1 ]
  [[ "$output" == *"❌ MyFitnessPal rejected the cookie"* ]] || false
  [[ "$output" == *"Those cookies didn't authenticate"* ]] || false
  [[ "$output" == *"MFP_USERNAME"* ]] || false
  [[ "$output" == *"chrome124"* ]] || false
  ! grep -qx 'mfp-mcp auth --check' "$STUB_LOG" || false
}

@test "a failing post-auth check exits 1" {
  echo 1 >"$STUB_DIR/auth.rc"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 1 ]
  [[ "$output" == *"❌ Saved, but the check failed: rejected"* ]] || false
}

@test "success restarts Mason only when it is loaded" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  ! grep -q 'launchctl kickstart' "$STUB_LOG" || false
  touch "$STUB_DIR/loaded-com.jdrami.mfp-mcp"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  grep -qx "launchctl kickstart -k gui/$(id -u)/com.jdrami.mfp-mcp" "$STUB_LOG"
  : >"$STUB_LOG"
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr --no-restart"
  ! grep -q 'launchctl kickstart' "$STUB_LOG" || false
}

@test "--dry-run validates input but runs nothing" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --dry-run --username jdr"
  [ "$status" -eq 0 ]
  [[ "$output" == *"<cookie on stdin> | MFP_USERNAME=jdr"* ]] || false
  [[ "$output" != *"SECRETCOOKIEVALUE"* ]] || false
  ! grep -q '^mfp-mcp' "$STUB_LOG" || false
}

@test "mfpctl auth pipes through end to end" {
  run bash -c "printf '%s' '$COOKIE' | '$REPO_ROOT/bin/mfpctl' auth --username jdr"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "$COOKIE" ]
  [[ "$output" == *"✅ Connected as"* ]] || false
}

# A realistic multi-cookie header: ';' separators, '=' inside values.
write_header() {
  printf 'Cookie: __Secure-next-auth.session-token=abc.SECRETTOKEN.def==; __cf_bm=x=SECRETCF; _ga=GA1.2.SECRETGA; p=1 \r\n' >"$BATS_TEST_TMPDIR/header"
}
HEADER_CLEAN='__Secure-next-auth.session-token=abc.SECRETTOKEN.def==; __cf_bm=x=SECRETCF; _ga=GA1.2.SECRETGA; p=1'

@test "a full Cookie header reaches Mason byte-for-byte, without a warning" {
  write_header
  run bash -c "'$AUTH' --username jdr <'$BATS_TEST_TMPDIR/header'"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "$HEADER_CLEAN" ]
  [[ "$output" == *"Input received (${#HEADER_CLEAN} characters, 4 cookies). Validating…"* ]] || false
  [[ "$output" != *"Only one cookie received"* ]] || false
}

@test "no value from a full header leaks to output, argv or env" {
  write_header
  run bash -c "'$AUTH' --username jdr <'$BATS_TEST_TMPDIR/header'"
  [ "$status" -eq 0 ]
  [[ "$output" != *SECRET* ]] || false
  ! grep -q SECRET "$STUB_LOG" || false
  ! grep -q SECRET "$STUB_DIR/auth-env" || false
}

@test "a bare token warns about single-cookie input but still authenticates" {
  run bash -c "printf '%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Only one cookie received"* ]] || false
  [[ "$output" == *"Request Headers → cookie → Copy value"* ]] || false
  [[ "$output" == *"✅ Connected as"* ]] || false
}

@test "a single name=value cookie also warns" {
  run bash -c "printf '__Secure-next-auth.session-token=%s' '$COOKIE' | '$AUTH' --username jdr"
  [ "$status" -eq 0 ]
  [[ "$output" == *"1 cookies"* ]] || false
  [[ "$output" == *"Only one cookie received"* ]] || false
}

@test "success reminds not to log out of MFP in that browser" {
  write_header
  run bash -c "'$AUTH' --username jdr <'$BATS_TEST_TMPDIR/header'"
  [[ "$output" == *"Don't log out of MyFitnessPal in the browser you copied this from."* ]] || false
}

@test "--help documents the full-header method and the log-out caveat" {
  run "$AUTH" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Request Headers"*"Copy value"* ]] || false
  [[ "$output" == *"Do not log out of MyFitnessPal in that browser"* ]] || false
  [[ "$output" == *"not recommended"* ]] || false
}

@test "__Host- cookies are dropped before Mason sees them; only the count is logged" {
  printf 'Cookie: __Secure-next-auth.session-token=abc.SECRETTOKEN; __Host-next-auth.csrf-token=SECRETCSRF%%7Cx; _ga=GA1.SECRETGA; __Host-other=SECRETH2; p=1' >"$BATS_TEST_TMPDIR/header"
  run bash -c "'$AUTH' --username jdr <'$BATS_TEST_TMPDIR/header'"
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_DIR/auth-stdin")" = "__Secure-next-auth.session-token=abc.SECRETTOKEN; _ga=GA1.SECRETGA; p=1" ]
  [[ "$output" == *"5 cookies"* ]] || false
  [[ "$output" == *"Dropped 2 __Host- cookies"* ]] || false
  [[ "$output" != *SECRET* ]] || false
  [[ "$output" != *"__Host-next-auth"* ]] || false
}

@test "input without __Host- cookies passes through unchanged and logs no drop" {
  write_header
  run bash -c "'$AUTH' --username jdr <'$BATS_TEST_TMPDIR/header'"
  [[ "$output" != *"Dropped"* ]] || false
}

@test "--help recommends the Chrome Guest window method" {
  run "$AUTH" --help
  [[ "$output" == *"Guest"* ]] || false
}
