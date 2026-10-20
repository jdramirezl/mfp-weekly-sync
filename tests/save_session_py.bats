#!/usr/bin/env bats
# scripts/save_session.py against fake myfitnesspal_mcp.auth / .refresh.

load helpers

setup() {
  [ -x /usr/bin/python3 ] || skip "needs /usr/bin/python3"
  common_setup
  SAVE="$REPO_ROOT/scripts/save_session.py"
  export PYTHONPATH="$REPO_ROOT/tests/fakepy" PYTHONDONTWRITEBYTECODE=1
  export FAKE_SAVE_FILE="$BATS_TEST_TMPDIR/saved.json" FAKE_SEED_FILE="$BATS_TEST_TMPDIR/seeded"
  export MFP_COOKIE_IN="$BATS_TEST_TMPDIR/in.json" MFP_USERNAME=jdr
  printf '{"cookies":{"__Secure-next-auth.session-token":"SECRET-SESSION","_ga":"x","__cf_bm":"y"}}' >"$MFP_COOKIE_IN"
}

@test "saves via auth.save_cookies with the username, then seeds Mason's profile" {
  run /usr/bin/python3 "$SAVE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Saved 3 cookies; Mason profile seeded."* ]] || false
  grep -q '"__Secure-next-auth.session-token": "SECRET-SESSION"' "$FAKE_SAVE_FILE"
  grep -q '"username": "jdr"' "$FAKE_SAVE_FILE"
  [ "$(cat "$FAKE_SEED_FILE")" = 3 ]
  [[ "$output" != *SECRET-SESSION* ]] || false
}

@test "a seeding failure keeps the saved session and warns" {
  FAKE_SEED_FAIL=1 run /usr/bin/python3 "$SAVE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARNING: seeding Mason's profile failed (RuntimeError)"* ]] || false
  [ -s "$FAKE_SAVE_FILE" ]
}

@test "no session cookie in the handoff saves nothing" {
  printf '{"cookies":{"_ga":"x"}}' >"$MFP_COOKIE_IN"
  run /usr/bin/python3 "$SAVE"
  [ "$status" -eq 1 ]
  [ ! -e "$FAKE_SAVE_FILE" ]
}

@test "a missing handoff file is exit 1" {
  rm "$MFP_COOKIE_IN"
  run /usr/bin/python3 "$SAVE"
  [ "$status" -eq 1 ]
}

@test "__Host- cookies are dropped before save_cookies and seed_profile" {
  printf '{"cookies":{"__Secure-next-auth.session-token":"S","__Host-next-auth.csrf-token":"C","_ga":"x"}}' >"$MFP_COOKIE_IN"
  run /usr/bin/python3 "$SAVE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Dropped 1 __Host- cookies"* ]] || false
  ! grep -q '__Host-' "$FAKE_SAVE_FILE" || false
  [ "$(cat "$FAKE_SEED_FILE")" = 2 ]
}
