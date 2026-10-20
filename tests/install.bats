#!/usr/bin/env bats
# scripts/install-mac.sh, against stubbed brew/uv and a fake HOME.

load helpers

setup() {
  common_setup
  INSTALL="$REPO_ROOT/scripts/install-mac.sh"
  LOGIN_VENV="$MFP_STATE_DIR/login-venv"
  export MFP_CHROME_APP="$TEST_HOME/Applications/Google Chrome.app"
  mkdir -p "$MFP_CHROME_APP"
  TOOLS_DIR="$TEST_HOME/uv-tools"
  cat >"$STUB_DIR/brew" <<'EOF'
#!/bin/bash
printf 'brew' >>"$STUB_LOG"; for a in "$@"; do printf ' %s' "$a" >>"$STUB_LOG"; done
printf ' [HOMEBREW_NO_AUTO_UPDATE=%s]\n' "${HOMEBREW_NO_AUTO_UPDATE:-}" >>"$STUB_LOG"
EOF
  chmod +x "$STUB_DIR/brew"
}

# curl + sh stubs standing in for the pinned standalone uv installer: curl
# writes a marker file, sh "runs" it by installing stub_uv at UV_INSTALL_DIR.
stub_uv_installer() {
  cat >"$STUB_DIR/curl" <<'EOF'
#!/bin/bash
printf 'curl' >>"$STUB_LOG"; for a in "$@"; do printf ' %s' "$a" >>"$STUB_LOG"; done; printf '\n' >>"$STUB_LOG"
while [ $# -gt 0 ]; do [ "$1" = -o ] && { echo '# uv installer' >"$2"; }; shift; done
EOF
  cat >"$STUB_DIR/sh" <<EOF
#!/bin/bash
printf 'sh %s [UV_INSTALL_DIR=%s]\n' "\$1" "\$UV_INSTALL_DIR" >>"\$STUB_LOG"
mkdir -p "\$UV_INSTALL_DIR" && cp "$STUB_DIR/uv.real" "\$UV_INSTALL_DIR/uv"
EOF
  chmod +x "$STUB_DIR/curl" "$STUB_DIR/sh"
  stub_uv
  mv "$STUB_DIR/uv" "$STUB_DIR/uv.real"
}

# A uv stub: `uv tool dir` -> $TOOLS_DIR; `uv tool list` -> contents of
# $STUB_DIR/uv-list; everything else just records the call.
stub_uv() {
  cat >"$STUB_DIR/uv" <<EOF
#!/bin/bash
printf 'uv' >>"\$STUB_LOG"; for a in "\$@"; do printf ' %s' "\$a" >>"\$STUB_LOG"; done; printf '\n' >>"\$STUB_LOG"
if [ "\$1 \$2" = "tool dir" ]; then echo "$TOOLS_DIR"; exit 0; fi
if [ "\$1 \$2" = "tool list" ]; then cat "\$STUB_DIR/uv-list" 2>/dev/null; exit 0; fi
if [ "\$1" = venv ]; then
  vdir="\${@: -1}"; mkdir -p "\$vdir/bin"
  cat >"\$vdir/bin/python" <<'PYEOF'
#!/bin/bash
printf 'login-python %s\\n' "\$*" >>"\$STUB_LOG"
case "\$*" in *patchright*) v="\$(cat "\$STUB_DIR/patchright-version" 2>/dev/null)"; [ "\$v" = 1.64.1 ] ;; *) exit 0 ;; esac
PYEOF
  chmod +x "\$vdir/bin/python"
fi
if [ "\$1 \$2" = "pip install" ]; then echo 1.64.1 >"\$STUB_DIR/patchright-version"; fi
if [ "\$1 \$2" = "tool install" ]; then
  mkdir -p "$TOOLS_DIR/mfp-mcp/bin"
  printf '#!/bin/bash\nprintf "tool-python %%s\\\\n" "\$*" >>"\$STUB_LOG"\n' >"$TOOLS_DIR/mfp-mcp/bin/python"
  chmod +x "$TOOLS_DIR/mfp-mcp/bin/python"
fi
exit 0
EOF
  chmod +x "$STUB_DIR/uv"
}

# Pretend mfp-mcp 0.4.1 with Playwright is already installed, on Python
# ${1:-3.13}: the stub fails the version probe unless that's 3.13.
fake_installed_tool() {
  mkdir -p "$TOOLS_DIR/mfp-mcp/bin"
  cat >"$TOOLS_DIR/mfp-mcp/bin/python" <<EOF
#!/bin/bash
printf 'tool-python' >>"\$STUB_LOG"; for a in "\$@"; do printf ' %s' "\$a" >>"\$STUB_LOG"; done; printf '\\n' >>"\$STUB_LOG"
case "\$*" in *version_info*) [ "${1:-3.13}" = 3.13 ] || exit 1 ;; esac
exit 0
EOF
  chmod +x "$TOOLS_DIR/mfp-mcp/bin/python"
  printf 'mfp-mcp v0.4.1\n- mfp-mcp\n- myfitnesspal-mcp\n' >"$STUB_DIR/uv-list"
}

@test "--dry-run prints the full sequence and creates nothing" {
  run "$INSTALL" --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ curl -fsSL --proto =https --tlsv1.2 -o "*"https://astral.sh/uv/0.13.1/install.sh"* ]] || false
  [[ "$output" == *"+ env UV_INSTALL_DIR=$TEST_HOME/.local/bin sh "* ]] || false
  [[ "$output" == *"/.local/bin/uv python install 3.13"* ]] || false
  [[ "$output" == *"/.local/bin/uv tool install --force --python 3.13 mfp-mcp\[autorefresh\]==0.4.1"* ]] || false
  [[ "$output" != *"brew install uv"* ]] || false
  [[ "$output" == *"-m playwright install chromium"* ]] || false
  [[ "$output" == *"+ chmod 700"*"myfitnesspal-mcp"* ]] || false
  [[ "$output" == *"mfp-mcp auth"* ]] || false
  [ ! -e "$TEST_HOME/Library" ]
  [ ! -e "$TEST_HOME/.config" ]
  # brew was only probed for, never invoked
  ! grep -q '^brew install' "$STUB_LOG" || false
}

@test "real run installs Mason and creates private folders" {
  stub_uv
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -qx 'uv python install 3.13' "$STUB_LOG"
  grep -qx 'uv tool install --force --python 3.13 mfp-mcp\[autorefresh\]==0.4.1' "$STUB_LOG"
  grep -q 'tool-python -m playwright install chromium' "$STUB_LOG"
  ! grep -q 'brew install' "$STUB_LOG" # uv already present || false
  [ "$(file_mode "$MFP_MCP_DATA_DIR")" = 700 ]
  [ "$(file_mode "$MFP_STATE_DIR")" = 700 ]
  [ "$(file_mode "$MFP_LOG_DIR")" = 700 ]
  [ "$(file_mode "$MFP_CONFIG_DIR")" = 700 ]
  [ "$(file_mode "$MFP_CONFIG_DIR/alert.env.example")" = 600 ]
  [ ! -e "$MFP_CONFIG_DIR/tunnel.env.example" ]
}

@test "second run is a no-op for the Mason install" {
  stub_uv
  fake_installed_tool
  run "$INSTALL"
  [ "$status" -eq 0 ]
  ! grep -q 'uv tool install' "$STUB_LOG" || false
  [[ "$output" == *"already installed"* ]] || false
  grep -qx 'tool-python -m playwright install chromium' "$STUB_LOG"
}

@test "reinstalls when the autorefresh extra is missing" {
  stub_uv
  fake_installed_tool
  rm "$TOOLS_DIR/mfp-mcp/bin/python"
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -q 'uv tool install --force' "$STUB_LOG"
}

@test "refuses to run on non-macOS" {
  stub_uname Linux
  run "$INSTALL" --dry-run
  [ "$status" -ne 0 ]
  [[ "$output" == *"macOS only"* ]] || false
}

@test "fresh Mac: installs uv from the pinned standalone installer, never brew" {
  stub_uv_installer
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -q "^curl -fsSL --proto =https --tlsv1.2 -o .* https://astral.sh/uv/0.13.1/install.sh$" "$STUB_LOG"
  grep -q "^sh .*uv-install\.[A-Za-z0-9]* \[UV_INSTALL_DIR=$TEST_HOME/.local/bin\]$" "$STUB_LOG"
  [ -x "$TEST_HOME/.local/bin/uv" ]
  grep -qx 'uv tool install --force --python 3.13 mfp-mcp\[autorefresh\]==0.4.1' "$STUB_LOG"
  ! grep -q '^brew' "$STUB_LOG" || false
  # the downloaded installer is cleaned up
  ! ls "${TMPDIR:-/tmp}"/uv-install.* 2>/dev/null | xargs grep -l '# uv installer' 2>/dev/null | grep -q . || false
}

@test "reuses uv already installed in ~/.local/bin" {
  stub_uv
  mkdir -p "$TEST_HOME/.local/bin"
  mv "$STUB_DIR/uv" "$TEST_HOME/.local/bin/uv"
  stub curl
  run "$INSTALL"
  [ "$status" -eq 0 ]
  ! grep -q '^curl' "$STUB_LOG" || false
  grep -q 'uv tool install --force --python 3.13' "$STUB_LOG"
}

@test "a tool built on Python 3.15 is reinstalled on 3.13" {
  stub_uv
  fake_installed_tool 3.15
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -qx 'uv tool install --force --python 3.13 mfp-mcp\[autorefresh\]==0.4.1' "$STUB_LOG"
}

@test "no Homebrew is fine by default" {
  rm "$STUB_DIR/brew"
  stub_uv
  run "$INSTALL"
  [ "$status" -eq 0 ]
}

@test "--with-tunnel without Homebrew fails clearly" {
  rm "$STUB_DIR/brew"
  stub_uv
  run "$INSTALL" --with-tunnel
  [ "$status" -ne 0 ]
  [[ "$output" == *"--with-tunnel needs Homebrew"* ]] || false
}

@test "brew runs with HOMEBREW_NO_AUTO_UPDATE=1" {
  stub_uv
  run "$INSTALL" --with-tunnel
  [ "$status" -eq 0 ]
  grep -qx 'brew install openai/tools/tunnel-client \[HOMEBREW_NO_AUTO_UPDATE=1\]' "$STUB_LOG"
}

@test "--with-tunnel adds the tunnel-client formula and example" {
  run "$INSTALL" --dry-run --with-tunnel
  [ "$status" -eq 0 ]
  [[ "$output" == *"HOMEBREW_NO_AUTO_UPDATE=1 brew install openai/tools/tunnel-client"* ]] || false
  [[ "$output" == *"tunnel.env.example"* ]] || false
}

@test "never touches an existing alert.env" {
  stub_uv
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'TELEGRAM_BOT_TOKEN=keep-me\n' >"$MFP_CONFIG_DIR/alert.env"
  chmod 600 "$MFP_CONFIG_DIR/alert.env"
  run "$INSTALL"
  [ "$status" -eq 0 ]
  [ "$(cat "$MFP_CONFIG_DIR/alert.env")" = "TELEGRAM_BOT_TOKEN=keep-me" ]
}

@test "never runs mfp-mcp auth" {
  stub_uv
  make_fake_mfp_mcp
  run "$INSTALL"
  [ "$status" -eq 0 ]
  ! grep -q '^mfp-mcp' "$STUB_LOG" || false
}

@test "--username writes MFP_USERNAME to a 0600 mason.env" {
  stub_uv
  run "$INSTALL" --username jdramirezl
  [ "$status" -eq 0 ]
  [ "$(cat "$MFP_CONFIG_DIR/mason.env")" = "MFP_USERNAME=jdramirezl" ]
  [ "$(file_mode "$MFP_CONFIG_DIR/mason.env")" = 600 ]
}

@test "--username appends to an existing mason.env, keeping its lines" {
  stub_uv
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_IMPERSONATE=chrome124' >"$MFP_CONFIG_DIR/mason.env"
  run "$INSTALL" --username jdr
  [ "$status" -eq 0 ]
  [ "$(cat "$MFP_CONFIG_DIR/mason.env")" = "$(printf 'MFP_IMPERSONATE=chrome124\nMFP_USERNAME=jdr')" ]
}

@test "an existing MFP_USERNAME is never overwritten" {
  stub_uv
  mkdir -p "$MFP_CONFIG_DIR"
  printf 'MFP_USERNAME=keep\n' >"$MFP_CONFIG_DIR/mason.env"
  run "$INSTALL" --username other
  [ "$status" -eq 0 ]
  [ "$(cat "$MFP_CONFIG_DIR/mason.env")" = "MFP_USERNAME=keep" ]
  [[ "$output" == *"already set"* ]] || false
}

@test "an email is rejected as the username" {
  stub_uv
  run "$INSTALL" --username me@example.com
  [ "$status" -ne 0 ]
  [[ "$output" == *"not your email"* ]] || false
}

@test "non-interactive run without a username warns and continues" {
  stub_uv
  run "$INSTALL" </dev/null
  [ "$status" -eq 0 ]
  [[ "$output" == *"need MFP_USERNAME"* ]] || false
  [ ! -e "$MFP_CONFIG_DIR/mason.env" ]
}

@test "interactive run prompts once for the username" {
  stub_uv
  run bash -c "printf 'promptuser\n' | MFP_INSTALL_ASSUME_TTY=1 '$INSTALL'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MyFitnessPal username (not email), Enter to skip"* ]] || false
  [ "$(cat "$MFP_CONFIG_DIR/mason.env")" = "MFP_USERNAME=promptuser" ]
}

@test "--dry-run shows the username write without doing it" {
  run "$INSTALL" --dry-run --username jdr
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ append MFP_USERNAME=jdr"* ]] || false
  [ ! -e "$MFP_CONFIG_DIR" ]
}

@test "creates the patchright login venv on Python 3.13 with the pinned version" {
  stub_uv
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -qx "uv venv --python 3.13 $LOGIN_VENV" "$STUB_LOG"
  grep -qx "uv pip install --python $LOGIN_VENV/bin/python patchright==1.64.1" "$STUB_LOG"
  ! grep -q 'patchright install' "$STUB_LOG" || false
}

@test "a correct patchright venv is left alone" {
  stub_uv
  "$INSTALL" >/dev/null 2>&1
  : >"$STUB_LOG"
  run "$INSTALL"
  [ "$status" -eq 0 ]
  ! grep -q '^uv venv' "$STUB_LOG" || false
  ! grep -q '^uv pip install' "$STUB_LOG" || false
  [[ "$output" == *"patchright 1.64.1 login venv already installed"* ]] || false
}

@test "a wrong patchright version is reinstalled" {
  stub_uv
  "$INSTALL" >/dev/null 2>&1
  echo 1.50.0 >"$STUB_DIR/patchright-version"
  : >"$STUB_LOG"
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -q '^uv pip install --python .*patchright==1.64.1' "$STUB_LOG"
}

@test "--dry-run shows the login venv steps and creates nothing" {
  run "$INSTALL" --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"uv venv --python 3.13 "*"login-venv"* ]] || false
  [[ "$output" == *"pip install --python "*"patchright==1.64.1"* ]] || false
  [ ! -e "$LOGIN_VENV" ]
}

@test "missing Google Chrome warns and continues" {
  stub_uv
  rm -rf "$MFP_CHROME_APP"
  run "$INSTALL"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Google Chrome not found"*"needed for mfpctl login"* ]] || false
}
