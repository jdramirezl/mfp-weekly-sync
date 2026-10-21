#!/usr/bin/env bats
# Docs stay in sync with the code they describe.

load helpers

doc_files() {
  printf '%s\n' "$REPO_ROOT/README.md" "$REPO_ROOT"/docs/*.md
}

@test "every 'mfpctl <command>' in the docs is a real command" {
  help="$("$REPO_ROOT/bin/mfpctl" help)"
  cmds="$(doc_files | xargs grep -ohE 'mfpctl [a-z][a-z-]*' | awk '{print $2}' | sort -u)"
  [ -n "$cmds" ]
  for c in $cmds; do
    if ! printf '%s\n' "$help" | grep -qE "^  ($c|tunnel $c)( |$)"; then
      echo "docs mention unknown command: mfpctl $c"
      return 1
    fi
  done
}

@test "every script path in the docs exists" {
  paths="$(doc_files | xargs grep -ohE '(^|[^./A-Za-z0-9_~-])(scripts|bin|launchd|config|plans)/[A-Za-z0-9_./-]*[A-Za-z0-9_]' | sed -E 's/^[^a-z]//' | sort -u)"
  [ -n "$paths" ]
  for p in $paths; do
    case "$p" in *'<'* | *'*'*) continue ;; esac
    if [ ! -e "$REPO_ROOT/$p" ]; then
      echo "docs reference a missing path: $p"
      return 1
    fi
  done
}

@test "no doc suggests a public bind or the removed PowerShell scripts" {
  run grep -l -E '0\.0\.0\.0|\.ps1' "$REPO_ROOT/README.md" "$REPO_ROOT"/docs/*.md "$REPO_ROOT"/plans/*.md
  [ "$status" -ne 0 ]
}

@test "mfpctl help lists every command" {
  run "$REPO_ROOT/bin/mfpctl" --help
  [ "$status" -eq 0 ]
  for c in install uninstall start stop restart status logs health auth refresh login "tunnel enable" "tunnel disable" "tunnel status"; do
    [[ "$output" == *"  $c"* ]] || false
  done
}

@test "runbook documents the full Cookie header and the log-out caveat" {
  grep -q 'Copy value' "$REPO_ROOT/docs/mac-runbook.md"
  grep -qi "don't log out\|do not log out" "$REPO_ROOT/docs/mac-runbook.md"
  ! grep -rq 'Application\*\* → \*\*Cookies' "$REPO_ROOT/README.md" "$REPO_ROOT/docs" || false
}

@test "runbook documents the bot-owned login and the Keychain prompt" {
  grep -q 'security add-generic-password -s mfp-login' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q 'Always Allow' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q 'mfpctl login' "$REPO_ROOT/README.md"
}

@test "runbook documents the Guest-window manual path" {
  grep -q 'Guest' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q '__Host-' "$REPO_ROOT/docs/mac-runbook.md"
}

@test "runbook documents the consent banner and exit 9" {
  grep -q 'consent banner dismissed' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q '  - 9: ' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q "tr -d '\\\\n' | wc -c" "$REPO_ROOT/docs/mac-runbook.md"
}

@test "runbook documents the cookie-rotation shim and the new re-login limits" {
  grep -q 'MFP_PERSIST_ROTATIONS=1' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q 'mfpctl rotation' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q 'never rate-limited' "$REPO_ROOT/docs/mac-runbook.md"
  grep -q 'MFP_PERSIST_ROTATIONS' "$REPO_ROOT/config/mason.env.example"
}
