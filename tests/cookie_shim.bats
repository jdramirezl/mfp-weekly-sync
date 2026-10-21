#!/usr/bin/env bats
# scripts/mason_entry.py + scripts/mason_cookie_shim.py against fake curl_cffi
# and fake Mason modules.

load helpers

setup() {
  [ -x /usr/bin/python3 ] || skip "needs /usr/bin/python3"
  common_setup
  ENTRY="$REPO_ROOT/scripts/mason_entry.py"
  export PYTHONPATH="$REPO_ROOT/tests/fakepy" PYTHONDONTWRITEBYTECODE=1
  export FAKE_CLI_LOG="$BATS_TEST_TMPDIR/cli.log" FAKE_SAVE_FILE="$BATS_TEST_TMPDIR/saved.json"
  export FAKE_COOKIES_JSON="$BATS_TEST_TMPDIR/cookies.json"
  printf '{"cookies":{"__Secure-next-auth.session-token":"OLD-SESSION-SECRET","remember_me":"OLD-RM-SECRET","_ga":"g","__Host-next-auth.csrf-token":"CSRF"}}' >"$FAKE_COOKIES_JSON"
  ROT="$MFP_LOG_DIR/cookie-rotation.log"
  unset MFP_PERSIST_ROTATIONS MFP_COOKIE_SHIM
}

check() { /usr/bin/python3 "$ENTRY" auth --check; }

@test "runs Mason's CLI with the same argv and exit code" {
  FAKE_CHECK_RC=3 run check
  [ "$status" -eq 3 ]
  [[ "$output" == *"Status:       valid"* ]] || false
  grep -qx 'cli argv0=mfp-mcp auth --check' "$FAKE_CLI_LOG"
  grep -q 'shim active' "$ROT"
}

@test "a remember_me rotation is logged once with hashes, never values" {
  FAKE_ROTATE='remember_me=NEW-RM-SECRET' run check
  [ "$status" -eq 0 ]
  [ "$(grep -c 'remember_me changed' "$ROT")" = 1 ]
  old=$(printf '%s' OLD-RM-SECRET | shasum -a 256 | cut -c1-8)
  new=$(printf '%s' NEW-RM-SECRET | shasum -a 256 | cut -c1-8)
  grep -q "/user/auth_token status=200 remember_me changed $old->$new" "$ROT"
  ! grep -rq SECRET "$MFP_LOG_DIR" || false
  [[ "$output" != *SECRET* ]] || false
  [ "$(file_mode "$ROT")" = 600 ]
  [ "$(file_mode "$MFP_LOG_DIR")" = 700 ]
}

@test "set and cleared are told apart; other hosts are ignored" {
  FAKE_ROTATE='_mfp_session=NEW-MS;remember_me=' FAKE_URL='https://example.com/x' run check
  grep -q '_mfp_session set -->' "$ROT"
  grep -q 'remember_me cleared ' "$ROT"
  ! grep -q 'example.com\|/x ' "$ROT" || false
}

@test "persistence is off by default: cookies.json untouched" {
  FAKE_ROTATE='remember_me=NEW-RM-SECRET' run check
  [ ! -e "$FAKE_SAVE_FILE" ]
  ! grep -q persisted "$ROT" || false
}

@test "MFP_PERSIST_ROTATIONS=1 merges the rotated jar via auth.save_cookies, minus __Host-" {
  MFP_PERSIST_ROTATIONS=1 FAKE_ROTATE='remember_me=NEW-RM-SECRET;__Host-x=H' run check
  [ "$status" -eq 0 ]
  /usr/bin/python3 - "$FAKE_SAVE_FILE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
c = d["cookies"]
assert c["remember_me"] == "NEW-RM-SECRET", c
assert c["__Secure-next-auth.session-token"] == "OLD-SESSION-SECRET"
assert c["_ga"] == "g"
assert not any(k.startswith("__Host-") for k in c), c
assert d["username"] == "jdr"
PY
  grep -q 'persisted rotation' "$ROT"
}

@test "a clear by MFP is never persisted" {
  MFP_PERSIST_ROTATIONS=1 FAKE_ROTATE='remember_me=;_mfp_session=NEW-MS' run check
  [ ! -e "$FAKE_SAVE_FILE" ]
  grep -q 'cleared by MFP: not persisted' "$ROT"
}

@test "a shim failure never breaks the request" {
  FAKE_NO_JAR=1 run check
  [ "$status" -eq 0 ]
  [[ "$output" == *"Status:       valid"* ]] || false
  grep -q 'shim error: AttributeError' "$ROT"
}

@test "MFP_COOKIE_SHIM=0 runs Mason without the shim" {
  MFP_COOKIE_SHIM=0 FAKE_ROTATE='remember_me=NEW' run check
  [ "$status" -eq 0 ]
  [ ! -e "$ROT" ]
}
