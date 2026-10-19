#!/usr/bin/env bash
# Shared bats setup: an isolated fake HOME, every path override pointed into
# it, and a stub bin/ that shadows external commands. Real Homebrew and uv are
# kept off PATH so a test can never touch the real machine.
# shellcheck disable=SC2034  # variables are consumed by the test files

REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"

common_setup() {
  TEST_HOME="${BATS_TEST_TMPDIR}/home"
  STUB_DIR="${BATS_TEST_TMPDIR}/stubs"
  STUB_LOG="${BATS_TEST_TMPDIR}/stub-calls.log"
  mkdir -p "$TEST_HOME" "$STUB_DIR"
  : >"$STUB_LOG"

  export MFP_HOME_OVERRIDE="$TEST_HOME"
  export LAUNCH_AGENTS_DIR="$TEST_HOME/Library/LaunchAgents"
  export MFP_MCP_DATA_DIR="$TEST_HOME/Library/Application Support/myfitnesspal-mcp"
  export MFP_STATE_DIR="$TEST_HOME/Library/Application Support/mfp-mason"
  export MFP_CONFIG_DIR="$TEST_HOME/.config/mfp-mason"
  export MFP_LOG_DIR="$TEST_HOME/Library/Logs/mfp-mason"
  export STUB_LOG STUB_DIR
  unset MFP_HOST MFP_PORT MFP_MCP_BIN TUNNEL_CLIENT_BIN MFP_IMPERSONATE MFP_USERNAME

  export PATH="$STUB_DIR:/usr/bin:/bin:/usr/sbin:/sbin"

  # Defaults every test gets: a Darwin uname and a launchctl that records
  # calls and reports nothing as loaded.
  stub_uname Darwin
  stub launchctl
}

# stub NAME [EXIT_CODE] [STDOUT]
# Records "NAME args..." in $STUB_LOG, prints STDOUT, exits EXIT_CODE.
# For `launchctl print`, exits 1 unless $STUB_DIR/loaded-<label> exists.
stub() {
  local name="$1" rc="${2:-0}" out="${3:-}"
  printf '%s' "$out" >"$STUB_DIR/$name.out"
  cat >"$STUB_DIR/$name" <<EOF
#!/bin/bash
printf '%s' "$name" >>"\$STUB_LOG"
for a in "\$@"; do printf ' %s' "\$a" >>"\$STUB_LOG"; done
printf '\n' >>"\$STUB_LOG"
if [ "$name" = launchctl ] && [ "\${1:-}" = print ]; then
  [ -e "\$STUB_DIR/loaded-\${2##*/}" ] && { cat "\$STUB_DIR/$name.out"; exit 0; }
  exit 1
fi
# launchctl state: bootstrap marks a label loaded, bootout unloads it.
if [ "$name" = launchctl ] && [ "\${1:-}" = bootstrap ]; then
  [ -e "\$STUB_DIR/launchctl-bootstrap-fails" ] && exit 5
  b="\${3##*/}"; touch "\$STUB_DIR/loaded-\${b%.plist}"
fi
if [ "$name" = launchctl ] && [ "\${1:-}" = bootout ]; then
  rm -f "\$STUB_DIR/loaded-\${2##*/}"
fi
cat "\$STUB_DIR/$name.out"
exit $rc
EOF
  chmod +x "$STUB_DIR/$name"
}

stub_uname() {
  cat >"$STUB_DIR/uname" <<EOF
#!/bin/bash
if [ "\${1:-}" = "-s" ] || [ \$# -eq 0 ]; then echo "$1"; exit 0; fi
exec /usr/bin/uname "\$@"
EOF
  chmod +x "$STUB_DIR/uname"
}

# A fake mfp-mcp binary at an absolute path, exported as MFP_MCP_BIN.
# Behaviour of `auth --check` is driven by $STUB_DIR/auth.rc.
make_fake_mfp_mcp() {
  local dir="$TEST_HOME/.local/bin"
  mkdir -p "$dir"
  cat >"$dir/mfp-mcp" <<'EOF'
#!/bin/bash
printf 'mfp-mcp' >>"$STUB_LOG"
for a in "$@"; do printf ' %s' "$a" >>"$STUB_LOG"; done
printf '\n' >>"$STUB_LOG"
if [ "${1:-}" = auth ] && [ "${2:-}" = --check ]; then
  rc="$(cat "$STUB_DIR/auth.rc" 2>/dev/null || echo 0)"
  echo "Session:      from /fake/cookies.json"
  if [ "$rc" = 0 ]; then
    echo "Status:       valid, connected as fakeuser"
  else
    echo "Status:       rejected (MyfitnesspalLoginError: expired)"
  fi
  echo "Auto-refresh: ready (profile at /fake/browser-profile)"
  exit "$rc"
fi
if [ "${1:-}" = auth ]; then
  # Interactive login: record what Mason would see, never echo the cookie.
  cat >"$STUB_DIR/auth-stdin"
  printf '%s' "${MFP_USERNAME:-}" >"$STUB_DIR/auth-username"
  env >"$STUB_DIR/auth-env"
  echo "Connect your MyFitnessPal account"
  echo "(no terminal detected — reading the cookie from stdin)" >&2
  echo "Validating with MyFitnessPal..."
  rc="$(cat "$STUB_DIR/auth-login.rc" 2>/dev/null || echo 0)"
  if [ "$rc" = 0 ]; then
    echo "Connected as ${MFP_USERNAME:-fakeuser}. Cookies saved to /fake/cookies.json"
    echo "Auto-refresh ready (profile at /fake/browser-profile)."
  else
    echo "Those cookies didn't authenticate: Authenticated, but couldn't read your MyFitnessPal profile." >&2
  fi
  exit "$rc"
fi
if [ "${1:-}" = --http ]; then
  # Service mode: dump argv + env so tests can inspect what launchd would run.
  printf '%s\n' "$*" >"$STUB_DIR/serve-argv"
  env >"$STUB_DIR/serve-env"
  exit 0
fi
exit 0
EOF
  chmod +x "$dir/mfp-mcp"
  export MFP_MCP_BIN="$dir/mfp-mcp"
}

calls() { cat "$STUB_LOG"; }

file_mode() { stat -f %Lp "$1"; }
