#!/usr/bin/env bats
# Repository shape after the legacy prune.

load helpers

@test "kept plan schema and example are valid JSON" {
  run plutil -convert json -o /dev/null "$REPO_ROOT/plans/weekly_plan.schema.json"
  [ "$status" -eq 0 ]
  run plutil -convert json -o /dev/null "$REPO_ROOT/plans/examples/weekly_plan.json"
  [ "$status" -eq 0 ]
}

@test "legacy Python package and PowerShell scripts are gone" {
  [ ! -e "$REPO_ROOT/src/mfp_sync" ]
  [ ! -e "$REPO_ROOT/pyproject.toml" ]
  run find "$REPO_ROOT" -path "$REPO_ROOT/.git" -prune -o -path "$REPO_ROOT/.tools" -prune -o -name '*.ps1' -print
  [ -z "$output" ]
}

@test ".gitignore keeps secrets and state out of git" {
  for pattern in '*.env' '*.state' '*.db' '*.log' 'cookies.json'; do
    grep -qxF "$pattern" "$REPO_ROOT/.gitignore"
  done
}

@test "reconciliation rules survive the prune" {
  grep -q "Three-way comparison" "$REPO_ROOT/docs/reconciliation-rules.md"
}
