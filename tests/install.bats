#!/usr/bin/env bats
# scripts/install-mac.sh, against stubbed brew/uv and a fake HOME.

load helpers

setup() {
  common_setup
  INSTALL="$REPO_ROOT/scripts/install-mac.sh"
  TOOLS_DIR="$TEST_HOME/uv-tools"
  stub brew
}

# A uv stub: `uv tool dir` -> $TOOLS_DIR; `uv tool list` -> contents of
# $STUB_DIR/uv-list; everything else just records the call.
stub_uv() {
  cat >"$STUB_DIR/uv" <<EOF
#!/bin/bash
printf 'uv' >>"\$STUB_LOG"; for a in "\$@"; do printf ' %s' "\$a" >>"\$STUB_LOG"; done; printf '\n' >>"\$STUB_LOG"
if [ "\$1 \$2" = "tool dir" ]; then echo "$TOOLS_DIR"; exit 0; fi
if [ "\$1 \$2" = "tool list" ]; then cat "\$STUB_DIR/uv-list" 2>/dev/null; exit 0; fi
if [ "\$1 \$2" = "tool install" ]; then
  mkdir -p "$TOOLS_DIR/mfp-mcp/bin"
  printf '#!/bin/bash\nprintf "tool-python %%s\\\\n" "\$*" >>"\$STUB_LOG"\n' >"$TOOLS_DIR/mfp-mcp/bin/python"
  chmod +x "$TOOLS_DIR/mfp-mcp/bin/python"
fi
exit 0
EOF
  chmod +x "$STUB_DIR/uv"
}

# Pretend mfp-mcp 0.4.1 with Playwright is already installed.
fake_installed_tool() {
  mkdir -p "$TOOLS_DIR/mfp-mcp/bin"
  cat >"$TOOLS_DIR/mfp-mcp/bin/python" <<'EOF'
#!/bin/bash
printf 'tool-python' >>"$STUB_LOG"; for a in "$@"; do printf ' %s' "$a" >>"$STUB_LOG"; done; printf '\n' >>"$STUB_LOG"
exit 0
EOF
  chmod +x "$TOOLS_DIR/mfp-mcp/bin/python"
  printf 'mfp-mcp v0.4.1\n- mfp-mcp\n- myfitnesspal-mcp\n' >"$STUB_DIR/uv-list"
}

@test "--dry-run prints the full sequence and creates nothing" {
  run "$INSTALL" --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ brew install uv"* ]]
  [[ "$output" == *"+ uv tool install --force mfp-mcp\[autorefresh\]==0.4.1"* ]]
  [[ "$output" == *"-m playwright install chromium"* ]]
  [[ "$output" == *"+ chmod 700"*"myfitnesspal-mcp"* ]]
  [[ "$output" == *"mfp-mcp auth"* ]]
  [ ! -e "$TEST_HOME/Library" ]
  [ ! -e "$TEST_HOME/.config" ]
  # brew was only probed for, never invoked
  ! grep -q '^brew install' "$STUB_LOG"
}

@test "real run installs Mason and creates private folders" {
  stub_uv
  run "$INSTALL"
  [ "$status" -eq 0 ]
  grep -qx 'uv tool install --force mfp-mcp\[autorefresh\]==0.4.1' "$STUB_LOG"
  grep -q 'tool-python -m playwright install chromium' "$STUB_LOG"
  ! grep -q 'brew install' "$STUB_LOG" # uv already present
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
  ! grep -q 'uv tool install' "$STUB_LOG"
  [[ "$output" == *"already installed"* ]]
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
  [[ "$output" == *"macOS only"* ]]
}

@test "fails clearly without Homebrew" {
  rm "$STUB_DIR/brew"
  run "$INSTALL" --dry-run
  [ "$status" -ne 0 ]
  [[ "$output" == *"Homebrew is required"* ]]
}

@test "--with-tunnel adds the tunnel-client formula and example" {
  run "$INSTALL" --dry-run --with-tunnel
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ brew install openai/tools/tunnel-client"* ]]
  [[ "$output" == *"tunnel.env.example"* ]]
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
  ! grep -q '^mfp-mcp' "$STUB_LOG"
}
