#!/bin/bash
# Runs every claude-config command against a temporary HOME and a temporary copy of the repo.
# The real HOME is never read or written: each case gets its own $T/home and $R.
# Usage: tests/run.sh [name-filter]
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
REAL_HOME="$HOME"
FILTER="${1:-}"
PASS=0
FAIL=0
T=""
R=""

ok() { PASS=$((PASS + 1)); echo "PASS $1"; }
ko() { FAIL=$((FAIL + 1)); echo "FAIL $1 — $2"; }

# Fresh temp repo (with fixtures) and empty temp HOME.
setup() {
  T="$(mktemp -d)"
  T="$(cd "$T" && pwd -P)"
  R="$T/home/tools/claude-config"   # the live clone: every symlink must point in here
  mkdir -p "$T/home" "$R/bin" "$R/global/skills/demo" "$R/global/memory" "$R/profiles/base"
  [ -f "$ROOT/bin/claude-config" ] && cp "$ROOT/bin/claude-config" "$R/bin/"
  echo "# global instructions" > "$R/global/CLAUDE.md"
  printf -- '---\nname: demo\n---\ndemo skill\n' > "$R/global/skills/demo/SKILL.md"
  echo "- index" > "$R/global/memory/MEMORY.md"
  cat > "$R/global/settings.base.json" <<'JSON'
{
  "model": "opus",
  "cleanupPeriodDays": 30,
  "permissions": {
    "allow": ["Bash(ls *)"],
    "ask": ["Bash(git push*)"],
    "deny": ["Read(~/.ssh/**)", "Bash(sudo *)"],
    "defaultMode": "auto"
  }
}
JSON
  cat > "$R/profiles/base/links.txt" <<'TXT'
# source                 target
global/CLAUDE.md         ~/.claude/CLAUDE.md
global/skills/demo       ~/.claude/skills/demo
global/memory            ~/.claude/memory
TXT
}

teardown() { [ -n "$T" ] && rm -rf "$T"; T=""; }

# Run the tool under macOS system bash (3.2) with the temp HOME.
cc() { HOME="$T/home" /bin/bash "$R/bin/claude-config" "$@"; }

# Stable listing of files and symlinks under a directory, with content checksums.
tree_sig() {
  (cd "$1" 2>/dev/null && find . \( -type f -o -type l \) | LC_ALL=C sort | while IFS= read -r f; do
    if [ -L "$f" ]; then echo "L $f -> $(readlink "$f")"; else echo "F $f $(cksum < "$f")"; fi
  done)
}

# The private repo's live clone next to the public one, with its own manifest and settings layer.
P=""
make_personal() {
  P="$T/home/tools/claude-personal"
  mkdir -p "$P/rules" "$P/skills/mine"
  echo "Reply in Ukrainian." > "$P/rules/personal.md"
  printf -- '---\nname: mine\n---\nmy skill\n' > "$P/skills/mine/SKILL.md"
  cat > "$P/links.txt" <<'TXT'
rules/personal.md        ~/.claude/rules/personal.md
skills/mine              ~/.claude/skills/mine
TXT
  cat > "$P/settings.personal.json" <<'JSON'
{
  "model": "opus",
  "theme": "dark",
  "enabledPlugins": { "superpowers@official": true },
  "permissions": { "ask": ["mcp__mail__send"], "deny": ["Read(~/.netrc)"] },
  "hooks": { "SubagentStart": [ { "matcher": "*", "hooks": [ { "type": "command", "command": "personal-reminder" } ] } ] }
}
JSON
}

# Run the tool from a working copy in ~/dev instead of the live clone.
cc_dev() {
  mkdir -p "$T/home/dev/claude-config/bin"
  cp "$R/bin/claude-config" "$T/home/dev/claude-config/bin/"
  HOME="$T/home" /bin/bash "$T/home/dev/claude-config/bin/claude-config" "$@"
}

# git without the user's global config, so tests never depend on or touch it
tgit() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -c user.name=test -c user.email=test@example.invalid -c init.defaultBranch=main "$@"; }

# Hub (bare repos instead of GitHub), working copies in ~/dev and live clones in ~/tools, all from the fixtures.
make_repos() {
  make_personal
  local name src
  for name in claude-config claude-personal; do
    src="$T/home/tools/$name"
    tgit init -q --bare "$T/hub/$name.git"
    tgit -C "$src" init -q && tgit -C "$src" add -A && tgit -C "$src" commit -qm "seed $name"
    tgit -C "$src" push -q "$T/hub/$name.git" HEAD:main
    rm -rf "$src"
    tgit clone -q "$T/hub/$name.git" "$src"
    tgit clone -q "$T/hub/$name.git" "$T/home/dev/$name"
  done
  mkdir -p "$T/bin"
  printf '#!/bin/bash\necho "gitleaks $*" >> "%s/gitleaks.log"\nexit "${FAKE_LEAKS:-0}"\n' "$T" > "$T/bin/gitleaks"
  chmod +x "$T/bin/gitleaks"
}

# sync as the agent would run it: no TTY, git identity without any global config, fake gitleaks.
cc_sync() {
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid \
  GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid CLAUDE_CONFIG_GITLEAKS="$T/bin/gitleaks" \
  HOME="$T/home" /bin/bash "$R/bin/claude-config" sync "$@" < /dev/null
}

hub_heads() { tgit -C "$T/hub/claude-config.git" rev-parse main; tgit -C "$T/hub/claude-personal.git" rev-parse main; }

dev_commit() {  # dev_commit <repo> <file> <content>
  echo "$3" > "$T/home/dev/$1/$2"
  tgit -C "$T/home/dev/$1" add -A && tgit -C "$T/home/dev/$1" commit -qm "dev: $2"
}

run_case() {
  local name="$1"
  if [ -n "$FILTER" ] && [ "${name#*"$FILTER"}" = "$name" ]; then return; fi
  setup
  "$name"
  teardown
}

# expect_rc <case> <wanted> <actual> <output>
expect_rc() {
  if [ "$3" = "$2" ]; then return 0; fi
  ko "$1" "exit $3, wanted $2; output: $(echo "$4" | tr '\n' '|' | cut -c1-"${OUTPUT_WIDTH:-300}")"
  return 1
}

# expect_has <case> <needle> <haystack>
expect_has() {
  case "$3" in *"$2"*) return 0 ;; esac
  ko "$1" "missing '$2' in: $(echo "$3" | tr '\n' '|' | cut -c1-300)"
  return 1
}

# ---------------------------------------------------------------- status

status_on_empty_home_reports_missing() {
  local out rc
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "MISSING" "$out" || return
  expect_has "$FUNCNAME" "~/.claude/CLAUDE.md" "$out" || return
  expect_has "$FUNCNAME" "settings: missing" "$out" || return
  ok "$FUNCNAME"
}

status_reports_replaced_and_foreign() {
  local out rc
  mkdir -p "$T/home/.claude/skills"
  echo "edited by hand" > "$T/home/.claude/CLAUDE.md"
  ln -s /nonexistent/elsewhere "$T/home/.claude/skills/demo"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "REPLACED" "$out" || return
  expect_has "$FUNCNAME" "FOREIGN" "$out" || return
  ok "$FUNCNAME"
}

status_reports_missing_source() {
  local out rc
  rm "$R/global/CLAUDE.md"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "SRC-MISSING" "$out" || return
  ok "$FUNCNAME"
}

manifest_with_bad_line_is_rejected() {
  local out rc
  echo "only-one-field" >> "$R/profiles/base/links.txt"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  ok "$FUNCNAME"
}

manifest_target_outside_allowed_dirs_is_rejected() {
  local out rc
  echo "global/CLAUDE.md ~/Documents/CLAUDE.md" >> "$R/profiles/base/links.txt"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  echo "global/CLAUDE.md ~/.claude/../.ssh/x" > "$R/profiles/base/links.txt"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  ok "$FUNCNAME"
}

unknown_command_is_usage_error() {
  local out rc
  out="$(cc frobnicate 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- link

link_on_empty_home_creates_symlinks_into_repo() {
  local out rc
  out="$(cc link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(readlink "$T/home/.claude/CLAUDE.md")" = "$R/global/CLAUDE.md" ] || { ko "$FUNCNAME" "CLAUDE.md is not a symlink into the repo"; return; }
  [ "$(readlink "$T/home/.claude/skills/demo")" = "$R/global/skills/demo" ] || { ko "$FUNCNAME" "skill dir is not a symlink into the repo"; return; }
  [ -f "$T/home/.claude/skills/demo/SKILL.md" ] || { ko "$FUNCNAME" "skill not readable through the link"; return; }
  out="$(cc status 2>&1)"
  case "$out" in *MISSING*|*REPLACED*|*FOREIGN*) ko "$FUNCNAME" "status after link: $out"; return ;; esac
  ok "$FUNCNAME"
}

link_twice_changes_nothing() {
  local before after out rc
  cc link >/dev/null 2>&1
  [ -L "$T/home/.claude/CLAUDE.md" ] || { ko "$FUNCNAME" "first link created nothing"; return; }
  before="$(tree_sig "$T/home")"
  out="$(cc link 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "second link changed HOME"; return; }
  ok "$FUNCNAME"
}

link_backs_up_identical_file_outside_claude_dir() {
  local out rc n
  mkdir -p "$T/home/.claude/skills"
  cp "$R/global/CLAUDE.md" "$T/home/.claude/CLAUDE.md"
  cp -R "$R/global/skills/demo" "$T/home/.claude/skills/demo"
  out="$(cc link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ -L "$T/home/.claude/CLAUDE.md" ] && [ -L "$T/home/.claude/skills/demo" ] || { ko "$FUNCNAME" "targets are not symlinks"; return; }
  n="$(find "$T/home/.local/state/claude-config/backups" -name CLAUDE.md -type f 2>/dev/null | wc -l | tr -d ' ')"
  [ "$n" = "1" ] || { ko "$FUNCNAME" "expected 1 backup of CLAUDE.md, found $n"; return; }
  n="$(find "$T/home/.local/state/claude-config/backups" -name SKILL.md -type f 2>/dev/null | wc -l | tr -d ' ')"
  [ "$n" = "1" ] || { ko "$FUNCNAME" "expected 1 backup of the skill, found $n"; return; }
  # nothing but the manifest targets may exist in ~/.claude (a stray copy of a skill dir would load as a second skill)
  n="$(find "$T/home/.claude" -mindepth 1 \( -type f -o -type d \) | grep -v -e '/skills$' | wc -l | tr -d ' ')"
  [ "$n" = "0" ] || { ko "$FUNCNAME" "unexpected real files left in ~/.claude: $(find "$T/home/.claude" -mindepth 1 -type f)"; return; }
  ok "$FUNCNAME"
}

link_refuses_when_existing_content_differs() {
  local out rc before after
  mkdir -p "$T/home/.claude"
  echo "edited by hand" > "$T/home/.claude/CLAUDE.md"
  before="$(tree_sig "$T/home")"
  out="$(cc link 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")"
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "HOME changed despite refusal"; return; }
  expect_has "$FUNCNAME" "pull" "$out" || return
  ok "$FUNCNAME"
}

link_refuses_foreign_symlink() {
  local out rc
  mkdir -p "$T/home/.claude"
  ln -s /nonexistent/elsewhere "$T/home/.claude/CLAUDE.md"
  out="$(cc link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$(readlink "$T/home/.claude/CLAUDE.md")" = "/nonexistent/elsewhere" ] || { ko "$FUNCNAME" "foreign symlink was replaced"; return; }
  ok "$FUNCNAME"
}

link_with_missing_source_is_manifest_error() {
  local out rc
  rm "$R/global/CLAUDE.md"
  out="$(cc link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  expect_has "$FUNCNAME" "source is missing" "$out" || return
  [ ! -e "$T/home/.claude" ] || { ko "$FUNCNAME" "link wrote to HOME despite the manifest error"; return; }
  ok "$FUNCNAME"
}

link_dry_run_writes_nothing() {
  local out rc before after
  mkdir -p "$T/home/.claude"
  cp "$R/global/CLAUDE.md" "$T/home/.claude/CLAUDE.md"
  before="$(tree_sig "$T/home")"
  out="$(cc link --dry-run 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "dry run changed HOME"; return; }
  expect_has "$FUNCNAME" "would" "$out" || return
  ok "$FUNCNAME"
}

file_created_in_linked_memory_dir_lands_in_repo() {
  local out
  cc link >/dev/null 2>&1
  echo "new memory" > "$T/home/.claude/memory/note.md"
  [ -f "$R/global/memory/note.md" ] || { ko "$FUNCNAME" "new file did not land in the repo"; return; }
  out="$(cc status 2>&1)"
  case "$out" in *"OK           ~/.claude/memory"*) ok "$FUNCNAME" ;; *) ko "$FUNCNAME" "memory not OK: $out" ;; esac
}

# ---------------------------------------------------------------- apply

live() { echo "$T/home/.claude/settings.json"; }
backups_of_settings() { find "$T/home/.local/state/claude-config/backups" -name settings.json -type f 2>/dev/null | wc -l | tr -d ' '; }

apply_on_clean_home_merges_base_and_machine() {
  local out rc
  mkdir -p "$T/home/.claude"
  cat > "$T/home/.claude/settings.machine.json" <<'JSON'
{ "theme": "dark", "permissions": { "allow": ["Bash(ls *)", "Bash(pwd)"], "deny": ["Read(~/.netrc)"] } }
JSON
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  jq -e . "$(live)" >/dev/null 2>&1 || { ko "$FUNCNAME" "live settings is not valid JSON"; return; }
  [ "$(jq -c '.permissions.allow' "$(live)")" = '["Bash(ls *)","Bash(pwd)"]' ] || { ko "$FUNCNAME" "allow not a union: $(jq -c '.permissions.allow' "$(live)")"; return; }
  [ "$(jq -c '.permissions.deny' "$(live)")" = '["Read(~/.ssh/**)","Bash(sudo *)","Read(~/.netrc)"]' ] || { ko "$FUNCNAME" "deny not a union: $(jq -c '.permissions.deny' "$(live)")"; return; }
  [ "$(jq -r '.theme + "/" + .model' "$(live)")" = "dark/opus" ] || { ko "$FUNCNAME" "scalars not merged"; return; }
  expect_has "$FUNCNAME" "Restart" "$out" || return
  ok "$FUNCNAME"
}

apply_twice_is_a_noop_without_a_backup() {
  local out rc before after
  cc apply >/dev/null 2>&1
  [ -f "$(live)" ] || { ko "$FUNCNAME" "first apply wrote nothing"; return; }
  before="$(tree_sig "$T/home")"
  out="$(cc apply 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "second apply changed HOME"; return; }
  [ "$(backups_of_settings)" = "0" ] || { ko "$FUNCNAME" "no-op apply created a backup"; return; }
  ok "$FUNCNAME"
}

apply_writes_repo_change_and_keeps_previous_file_as_backup() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.cleanupPeriodDays = 90' "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq '.cleanupPeriodDays' "$(live)")" = "90" ] || { ko "$FUNCNAME" "repo change not applied"; return; }
  [ "$(backups_of_settings)" = "1" ] || { ko "$FUNCNAME" "expected 1 backup, found $(backups_of_settings)"; return; }
  [ "$(find "$T/home/.claude" -type f | wc -l | tr -d ' ')" = "1" ] || { ko "$FUNCNAME" "stray files in ~/.claude: $(find "$T/home/.claude" -type f)"; return; }
  ok "$FUNCNAME"
}

apply_refuses_when_live_has_a_deny_the_repo_lacks() {
  local out rc before after
  cc apply >/dev/null 2>&1
  jq '.permissions.deny += ["Read(~/.aws/**)"]' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  before="$(cksum < "$(live)")"
  out="$(cc apply 2>&1)"; rc=$?
  after="$(cksum < "$(live)")"
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "live settings changed despite refusal"; return; }
  expect_has "$FUNCNAME" "Read(~/.aws/**)" "$out" || return
  ok "$FUNCNAME"
}

apply_refuses_when_repo_drops_a_deny() {
  local out rc before after
  cc apply >/dev/null 2>&1
  jq '.permissions.deny -= ["Bash(sudo *)"]' "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  before="$(cksum < "$(live)")"
  out="$(cc apply 2>&1)"; rc=$?
  after="$(cksum < "$(live)")"
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "live settings changed despite refusal"; return; }
  expect_has "$FUNCNAME" "Bash(sudo *)" "$out" || return
  # a deliberate removal has exactly one path, and the refusal must name it
  expect_has "$FUNCNAME" "by hand" "$out" || return
  expect_has "$FUNCNAME" "--discard-live" "$out" || return
  ok "$FUNCNAME"
}

apply_discard_live_never_overrides_a_deny_loss() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.permissions.deny += ["Read(~/.aws/**)"]' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc apply --discard-live 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$(jq -r '.permissions.deny | index("Read(~/.aws/**)") != null' "$(live)")" = "true" ] || { ko "$FUNCNAME" "deny rule was dropped"; return; }
  ok "$FUNCNAME"
}

apply_refuses_when_live_changed_outside_the_tool() {
  local out rc before after
  cc apply >/dev/null 2>&1
  jq '.permissions.allow += ["Bash(date)"] | .enabledPlugins = {"x@y": true}' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  before="$(cksum < "$(live)")"
  out="$(cc apply 2>&1)"; rc=$?
  after="$(cksum < "$(live)")"
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "live settings changed despite refusal"; return; }
  expect_has "$FUNCNAME" "pull" "$out" || return
  out="$(cc apply --discard-live 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq 'has("enabledPlugins")' "$(live)")" = "false" ] || { ko "$FUNCNAME" "--discard-live kept the live-only key"; return; }
  [ "$(backups_of_settings)" = "1" ] || { ko "$FUNCNAME" "discarded live file was not backed up"; return; }
  ok "$FUNCNAME"
}

apply_first_run_refuses_an_unknown_live_file() {
  local out rc
  mkdir -p "$T/home/.claude"
  echo '{"model":"sonnet","permissions":{"deny":["Read(~/.ssh/**)","Bash(sudo *)"]}}' > "$(live)"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$(jq -r .model "$(live)")" = "sonnet" ] || { ko "$FUNCNAME" "live settings was overwritten"; return; }
  ok "$FUNCNAME"
}

apply_first_run_adopts_an_equivalent_live_file_untouched() {
  local out rc before after
  mkdir -p "$T/home/.claude"
  # same content as the base, different key order and list order
  jq -S '.permissions.deny |= reverse' "$R/global/settings.base.json" > "$(live)"
  before="$(cksum < "$(live)")"
  out="$(cc apply 2>&1)"; rc=$?
  after="$(cksum < "$(live)")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "equivalent live file was rewritten"; return; }
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "settings: in sync" "$out" || return
  ok "$FUNCNAME"
}

apply_refuses_symlinked_settings() {
  local out rc
  mkdir -p "$T/home/.claude"
  ln -s "$R/global/settings.base.json" "$(live)"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ -L "$(live)" ] || { ko "$FUNCNAME" "symlink was replaced"; return; }
  ok "$FUNCNAME"
}

apply_with_invalid_base_json_is_an_error() {
  local out rc
  echo '{ not json' > "$R/global/settings.base.json"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  expect_has "$FUNCNAME" "not valid JSON" "$out" || return
  [ ! -e "$(live)" ] || { ko "$FUNCNAME" "wrote settings from invalid base"; return; }
  ok "$FUNCNAME"
}

apply_dry_run_writes_nothing() {
  local out rc before after
  before="$(tree_sig "$T/home")"
  out="$(cc apply --dry-run 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "dry run changed HOME"; return; }
  expect_has "$FUNCNAME" "would" "$out" || return
  ok "$FUNCNAME"
}

status_is_clean_after_link_and_apply() {
  local out rc
  cc link >/dev/null 2>&1; cc apply >/dev/null 2>&1
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  expect_has "$FUNCNAME" "settings: in sync" "$out" || return
  ok "$FUNCNAME"
}

status_tells_drift_from_pending_and_deny_loss() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.cleanupPeriodDays = 90' "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "settings: pending" "$out" || return
  jq '.theme = "light"' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "settings: drift" "$out" || return
  jq '.permissions.deny += ["Read(~/.aws/**)"]' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "settings: deny-loss" "$out" || return
  rm "$(live)"; ln -s "$R/global/settings.base.json" "$(live)"
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "settings: is-symlink" "$out" || return
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- diff, pull, unlink

# What Claude Code does to a symlinked file when it saves atomically: temp file, then rename over the link.
atomic_write() { printf '%s\n' "$2" > "$1.tmp" && mv "$1.tmp" "$1"; }

diff_is_clean_when_everything_is_in_sync() {
  local out rc
  cc link >/dev/null 2>&1; cc apply >/dev/null 2>&1
  out="$(cc diff 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  ok "$FUNCNAME"
}

diff_shows_settings_and_file_changes_without_writing() {
  local out rc before after
  cc link >/dev/null 2>&1; cc apply >/dev/null 2>&1
  jq '.theme = "light"' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  atomic_write "$T/home/.claude/CLAUDE.md" "# edited live"
  before="$(tree_sig "$T/home")$(tree_sig "$R")"
  out="$(cc diff 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")$(tree_sig "$R")"
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" '"theme": "light"' "$out" || return
  expect_has "$FUNCNAME" "# edited live" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "diff wrote something"; return; }
  ok "$FUNCNAME"
}

symlink_replaced_by_file_is_detected_and_pulled_back() {
  local out rc
  cc link >/dev/null 2>&1
  atomic_write "$T/home/.claude/CLAUDE.md" "# edited live"
  [ ! -L "$T/home/.claude/CLAUDE.md" ] || { ko "$FUNCNAME" "fixture failed: still a symlink"; return; }
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "REPLACED     ~/.claude/CLAUDE.md" "$out" || return
  out="$(cc pull --relink 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(cat "$R/global/CLAUDE.md")" = "# edited live" ] || { ko "$FUNCNAME" "live edit did not reach the repo"; return; }
  [ "$(readlink "$T/home/.claude/CLAUDE.md")" = "$R/global/CLAUDE.md" ] || { ko "$FUNCNAME" "link was not restored"; return; }
  ok "$FUNCNAME"
}

pull_without_relink_leaves_the_live_file_alone() {
  local out rc
  cc link >/dev/null 2>&1
  atomic_write "$T/home/.claude/CLAUDE.md" "# edited live"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(cat "$R/global/CLAUDE.md")" = "# edited live" ] || { ko "$FUNCNAME" "live edit did not reach the repo"; return; }
  [ ! -L "$T/home/.claude/CLAUDE.md" ] || { ko "$FUNCNAME" "pull relinked without --relink"; return; }
  ok "$FUNCNAME"
}

pull_brings_live_settings_changes_into_base_then_apply_passes() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.permissions.allow += ["Bash(date)"] | .enabledPlugins = {"x@y": true}' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq -c '.enabledPlugins' "$R/global/settings.base.json")" = '{"x@y":true}' ] || { ko "$FUNCNAME" "live key missing from base"; return; }
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "settings: in sync" "$out" || return
  ok "$FUNCNAME"
}

pull_brings_an_extra_live_deny_into_base() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.permissions.deny += ["Read(~/.aws/**)"]' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq -r '.permissions.deny | index("Read(~/.aws/**)") != null' "$R/global/settings.base.json")" = "true" ] || { ko "$FUNCNAME" "deny rule missing from base"; return; }
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  ok "$FUNCNAME"
}

pull_keeps_machine_keys_out_of_base() {
  local out rc
  mkdir -p "$T/home/.claude"
  echo '{ "theme": "dark", "permissions": { "allow": ["Bash(pwd)"] } }' > "$T/home/.claude/settings.machine.json"
  cc apply >/dev/null 2>&1
  jq '.enabledPlugins = {"x@y": true}' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq 'has("theme")' "$R/global/settings.base.json")" = "false" ] || { ko "$FUNCNAME" "machine key leaked into base"; return; }
  [ "$(jq -c '.permissions.allow' "$R/global/settings.base.json")" = '["Bash(ls *)"]' ] || { ko "$FUNCNAME" "machine allow leaked into base: $(jq -c '.permissions.allow' "$R/global/settings.base.json")"; return; }
  [ "$(jq 'has("enabledPlugins")' "$R/global/settings.base.json")" = "true" ] || { ko "$FUNCNAME" "live key missing from base"; return; }
  ok "$FUNCNAME"
}

pull_refuses_to_drop_a_deny_from_base() {
  local out rc before after
  cc apply >/dev/null 2>&1
  jq '.permissions.deny -= ["Bash(sudo *)"]' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  before="$(cksum < "$R/global/settings.base.json")"
  out="$(cc pull 2>&1)"; rc=$?
  after="$(cksum < "$R/global/settings.base.json")"
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "base changed despite refusal"; return; }
  ok "$FUNCNAME"
}

pull_does_not_revert_a_pending_repo_change() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.cleanupPeriodDays = 90' "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq '.cleanupPeriodDays' "$R/global/settings.base.json")" = "90" ] || { ko "$FUNCNAME" "pull reverted the repo change"; return; }
  ok "$FUNCNAME"
}

pull_refuses_when_repo_and_live_both_changed() {
  local out rc
  cc apply >/dev/null 2>&1
  jq '.cleanupPeriodDays = 90' "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  jq '.theme = "light"' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$(jq '.cleanupPeriodDays' "$R/global/settings.base.json")" = "90" ] || { ko "$FUNCNAME" "base was overwritten"; return; }
  ok "$FUNCNAME"
}

pull_dry_run_writes_nothing() {
  local out rc before after
  cc link >/dev/null 2>&1; cc apply >/dev/null 2>&1
  jq '.theme = "light"' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  atomic_write "$T/home/.claude/CLAUDE.md" "# edited live"
  before="$(tree_sig "$T/home")$(tree_sig "$R")"
  out="$(cc pull --relink --dry-run 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")$(tree_sig "$R")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "dry run wrote something"; return; }
  expect_has "$FUNCNAME" "would" "$out" || return
  ok "$FUNCNAME"
}

unlink_replaces_symlinks_with_identical_copies() {
  local out rc n
  cc link >/dev/null 2>&1
  out="$(cc unlink 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  n="$(find "$T/home/.claude" -type l | wc -l | tr -d ' ')"
  [ "$n" = "0" ] || { ko "$FUNCNAME" "$n symlinks left"; return; }
  cmp -s "$T/home/.claude/CLAUDE.md" "$R/global/CLAUDE.md" || { ko "$FUNCNAME" "file copy differs"; return; }
  diff -rq "$T/home/.claude/skills/demo" "$R/global/skills/demo" >/dev/null || { ko "$FUNCNAME" "dir copy differs"; return; }
  ok "$FUNCNAME"
}

unlink_dry_run_writes_nothing() {
  local out rc before after
  cc link >/dev/null 2>&1
  before="$(tree_sig "$T/home")"
  out="$(cc unlink --dry-run 2>&1)"; rc=$?
  after="$(tree_sig "$T/home")"
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "dry run changed HOME"; return; }
  expect_has "$FUNCNAME" "would" "$out" || return
  ok "$FUNCNAME"
}

tool_writes_only_to_its_three_locations() {
  local out stray
  mkdir -p "$T/home/.claude"
  cp "$R/global/CLAUDE.md" "$T/home/.claude/CLAUDE.md"
  cc link >/dev/null 2>&1; cc apply >/dev/null 2>&1
  jq '.cleanupPeriodDays = 90' "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  cc apply >/dev/null 2>&1
  atomic_write "$T/home/.claude/CLAUDE.md" "# edited live"
  cc pull --relink >/dev/null 2>&1
  cc unlink >/dev/null 2>&1
  [ -f "$T/home/.claude/settings.json" ] || { ko "$FUNCNAME" "scenario did not run (no settings.json)"; return; }
  stray="$(cd "$T/home" && find . -mindepth 1 \( -type f -o -type l \) | grep -v -e '^\./\.claude/' -e '^\./\.config/claude-kit' -e '^\./\.local/state/claude-config/' -e '^\./tools/')"
  [ -z "$stray" ] || { ko "$FUNCNAME" "wrote outside the allowed locations: $stray"; return; }
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- orphans

status_warns_about_a_link_dropped_from_the_manifest() {
  local out rc
  cc link >/dev/null 2>&1
  grep -v '^global/CLAUDE.md' "$R/profiles/base/links.txt" > "$T/m" && mv "$T/m" "$R/profiles/base/links.txt"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "ORPHAN       ~/.claude/CLAUDE.md" "$out" || return
  ok "$FUNCNAME"
}

status_warns_about_a_dangling_link_into_the_repo() {
  local out rc
  cc link >/dev/null 2>&1
  mkdir -p "$T/home/.claude/rules"
  ln -s "$R/global/rules/old.md" "$T/home/.claude/rules/old.md"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "ORPHAN       ~/.claude/rules/old.md" "$out" || return
  ok "$FUNCNAME"
}

status_ignores_links_that_point_elsewhere() {
  local out rc
  cc link >/dev/null 2>&1; cc apply >/dev/null 2>&1
  mkdir -p "$T/home/.claude/rules"
  ln -s /nonexistent/elsewhere.md "$T/home/.claude/rules/elsewhere.md"
  out="$(cc status 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  case "$out" in *ORPHAN*) ko "$FUNCNAME" "foreign link reported as orphan: $out"; return ;; esac
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- v2: two repos, live root, three layers

links_from_a_working_copy_still_point_into_the_live_clone() {
  local out rc
  out="$(cc_dev link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(readlink "$T/home/.claude/CLAUDE.md")" = "$R/global/CLAUDE.md" ] || { ko "$FUNCNAME" "link points to $(readlink "$T/home/.claude/CLAUDE.md")"; return; }
  ok "$FUNCNAME"
}

live_root_inside_dev_is_refused() {
  local out rc
  mkdir -p "$T/home/dev/tools"
  out="$(CLAUDE_CONFIG_LIVE_ROOT="$T/home/dev/tools" cc link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  [ ! -e "$T/home/.claude" ] || { ko "$FUNCNAME" "wrote to HOME"; return; }
  ok "$FUNCNAME"
}

personal_manifest_links_into_the_personal_live_clone() {
  local out rc
  make_personal
  out="$(cc link 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(readlink "$T/home/.claude/rules/personal.md")" = "$P/rules/personal.md" ] || { ko "$FUNCNAME" "rule link: $(readlink "$T/home/.claude/rules/personal.md")"; return; }
  [ "$(readlink "$T/home/.claude/skills/mine")" = "$P/skills/mine" ] || { ko "$FUNCNAME" "skill link wrong"; return; }
  out="$(cc status 2>&1)"
  expect_has "$FUNCNAME" "OK           ~/.claude/rules/personal.md" "$out" || return
  ok "$FUNCNAME"
}

apply_merges_base_personal_and_machine() {
  local out rc
  make_personal
  mkdir -p "$T/home/.claude"
  echo '{ "theme": "light", "permissions": { "allow": ["Bash(pwd)"] } }' > "$T/home/.claude/settings.machine.json"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq -r '.theme' "$(live)")" = "light" ] || { ko "$FUNCNAME" "machine scalar did not win: $(jq -r .theme "$(live)")"; return; }
  [ "$(jq -r '.cleanupPeriodDays' "$(live)")" = "30" ] || { ko "$FUNCNAME" "base scalar lost"; return; }
  [ "$(jq -c '.enabledPlugins' "$(live)")" = '{"superpowers@official":true}' ] || { ko "$FUNCNAME" "personal object missing"; return; }
  [ "$(jq -c '.permissions.ask' "$(live)")" = '["Bash(git push*)","mcp__mail__send"]' ] || { ko "$FUNCNAME" "ask not united: $(jq -c '.permissions.ask' "$(live)")"; return; }
  [ "$(jq -c '.permissions.deny' "$(live)")" = '["Read(~/.ssh/**)","Bash(sudo *)","Read(~/.netrc)"]' ] || { ko "$FUNCNAME" "deny not united"; return; }
  [ "$(jq -c '.permissions.allow' "$(live)")" = '["Bash(ls *)","Bash(pwd)"]' ] || { ko "$FUNCNAME" "allow not united"; return; }
  ok "$FUNCNAME"
}

hooks_from_every_layer_are_united_without_duplicates() {
  local out rc
  make_personal
  jq '.hooks = { "SessionStart": [ { "matcher": "startup", "hooks": [ { "type": "command", "command": "base-start" } ] } ],
                 "SubagentStart": [ { "matcher": "*", "hooks": [ { "type": "command", "command": "personal-reminder" } ] } ] }' \
    "$R/global/settings.base.json" > "$T/b" && mv "$T/b" "$R/global/settings.base.json"
  jq '.hooks.SessionStart = [ { "matcher": "startup", "hooks": [ { "type": "command", "command": "personal-start" } ] } ]' \
    "$P/settings.personal.json" > "$T/p" && mv "$T/p" "$P/settings.personal.json"
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq -c '[.hooks.SessionStart[].hooks[].command]' "$(live)")" = '["base-start","personal-start"]' ] || { ko "$FUNCNAME" "SessionStart: $(jq -c '[.hooks.SessionStart[].hooks[].command]' "$(live)")"; return; }
  [ "$(jq -c '[.hooks.SubagentStart[].hooks[].command]' "$(live)")" = '["personal-reminder"]' ] || { ko "$FUNCNAME" "duplicate kept: $(jq -c '[.hooks.SubagentStart[].hooks[].command]' "$(live)")"; return; }
  ok "$FUNCNAME"
}

apply_refuses_when_the_personal_layer_drops_a_live_deny() {
  local out rc before after
  make_personal
  cc apply >/dev/null 2>&1
  jq '.permissions.deny = []' "$P/settings.personal.json" > "$T/p" && mv "$T/p" "$P/settings.personal.json"
  before="$(cksum < "$(live)")"
  out="$(cc apply 2>&1)"; rc=$?
  after="$(cksum < "$(live)")"
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$after" ] || { ko "$FUNCNAME" "live changed despite refusal"; return; }
  expect_has "$FUNCNAME" "Read(~/.netrc)" "$out" || return
  ok "$FUNCNAME"
}

pull_with_a_personal_repo_writes_a_minimal_personal_overlay() {
  local out rc
  make_personal
  cc apply >/dev/null 2>&1
  jq '.enabledPlugins["x@y"] = true | .permissions.allow += ["Bash(date)"] | .theme = "light"' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  local base_before; base_before="$(cksum < "$R/global/settings.base.json")"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$base_before" = "$(cksum < "$R/global/settings.base.json")" ] || { ko "$FUNCNAME" "base changed"; return; }
  [ "$(jq -c '.enabledPlugins' "$P/settings.personal.json")" = '{"superpowers@official":true,"x@y":true}' ] || { ko "$FUNCNAME" "plugins: $(jq -c .enabledPlugins "$P/settings.personal.json")"; return; }
  [ "$(jq -r '.theme' "$P/settings.personal.json")" = "light" ] || { ko "$FUNCNAME" "theme not pulled"; return; }
  [ "$(jq -c '.permissions.allow' "$P/settings.personal.json")" = '["Bash(date)"]' ] || { ko "$FUNCNAME" "allow overlay: $(jq -c .permissions.allow "$P/settings.personal.json")"; return; }
  [ "$(jq 'has("cleanupPeriodDays")' "$P/settings.personal.json")" = "false" ] || { ko "$FUNCNAME" "base key copied into personal"; return; }
  out="$(cc apply 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  expect_has "$FUNCNAME" "already in sync" "$out" || return
  ok "$FUNCNAME"
}

pull_keeps_machine_values_out_of_the_personal_layer() {
  local out rc
  make_personal
  mkdir -p "$T/home/.claude"
  echo '{ "tui": "fullscreen", "permissions": { "allow": ["Bash(pwd)"] } }' > "$T/home/.claude/settings.machine.json"
  cc apply >/dev/null 2>&1
  jq '.enabledPlugins["x@y"] = true' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  [ "$(jq 'has("tui")' "$P/settings.personal.json")" = "false" ] || { ko "$FUNCNAME" "machine scalar leaked"; return; }
  [ "$(jq -c '.permissions.allow // []' "$P/settings.personal.json")" = '[]' ] || { ko "$FUNCNAME" "machine allow leaked"; return; }
  ok "$FUNCNAME"
}

pull_refuses_to_drop_a_personal_deny() {
  local out rc before
  make_personal
  cc apply >/dev/null 2>&1
  jq '.permissions.deny -= ["Read(~/.netrc)"]' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  before="$(cksum < "$P/settings.personal.json")"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$(cksum < "$P/settings.personal.json")" ] || { ko "$FUNCNAME" "personal changed despite refusal"; return; }
  expect_has "$FUNCNAME" "Read(~/.netrc)" "$out" || return
  ok "$FUNCNAME"
}

pull_refuses_a_change_the_personal_layer_cannot_express() {
  local out rc before
  make_personal
  cc apply >/dev/null 2>&1
  jq 'del(.cleanupPeriodDays)' "$(live)" > "$T/l" && mv "$T/l" "$(live)"
  before="$(cksum < "$P/settings.personal.json")"
  out="$(cc pull 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$(cksum < "$P/settings.personal.json")" ] || { ko "$FUNCNAME" "personal changed despite refusal"; return; }
  expect_has "$FUNCNAME" "cleanupPeriodDays" "$out" || return
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- personal_dirty SessionStart hook

personal_dirty_hook_speaks_only_when_the_personal_clone_has_changes() {
  local hook="$ROOT/claude-kit/hooks/personal_dirty.sh" out rc
  out="$(HOME="$T/home" /bin/bash "$hook" 2>&1)"; rc=$?
  [ "$rc$out" = "0" ] || { ko "$FUNCNAME" "no repo: rc=$rc out=$out"; return; }
  make_personal
  tgit -C "$P" init -q && tgit -C "$P" add -A && tgit -C "$P" commit -qm init
  out="$(HOME="$T/home" /bin/bash "$hook" 2>&1)"; rc=$?
  [ "$rc$out" = "0" ] || { ko "$FUNCNAME" "clean repo: rc=$rc out=$out"; return; }
  echo "new memory" > "$P/rules/note.md"
  out="$(HOME="$T/home" /bin/bash "$hook" 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  expect_has "$FUNCNAME" "claude-config sync" "$out" || return
  [ "$(echo "$out" | wc -l | tr -d ' ')" = "1" ] || { ko "$FUNCNAME" "more than one line: $out"; return; }
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- sync

sync_without_yes_shows_the_plan_and_changes_nothing() {
  local out rc before
  make_repos
  echo "agent memory" > "$T/home/tools/claude-personal/rules/agent-note.md"
  dev_commit claude-config global/NEW.md "new general file"
  before="$(hub_heads)"
  out="$(cc_sync 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  [ "$before" = "$(hub_heads)" ] || { ko "$FUNCNAME" "hub changed without confirmation"; return; }
  [ -n "$(tgit -C "$T/home/tools/claude-personal" status --porcelain)" ] || { ko "$FUNCNAME" "live changes were committed"; return; }
  expect_has "$FUNCNAME" "agent-note.md" "$out" || return
  expect_has "$FUNCNAME" "dev: global/NEW.md" "$out" || return
  expect_has "$FUNCNAME" "--yes" "$out" || return
  ok "$FUNCNAME"
}

sync_yes_pushes_live_and_dev_changes_then_pulls_links_and_applies() {
  local out rc
  make_repos
  echo "agent memory" > "$T/home/tools/claude-personal/rules/agent-note.md"
  dev_commit claude-config global/NEW.md "new general file"
  out="$(cc_sync --yes 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  tgit -C "$T/hub/claude-personal.git" show main:rules/agent-note.md >/dev/null 2>&1 || { ko "$FUNCNAME" "live change not pushed"; return; }
  tgit -C "$T/hub/claude-config.git" show main:global/NEW.md >/dev/null 2>&1 || { ko "$FUNCNAME" "dev commit not pushed"; return; }
  [ -f "$T/home/tools/claude-config/global/NEW.md" ] || { ko "$FUNCNAME" "live clone not pulled"; return; }
  [ -z "$(tgit -C "$T/home/tools/claude-personal" status --porcelain)" ] || { ko "$FUNCNAME" "live clone still dirty"; return; }
  [ -L "$T/home/.claude/rules/personal.md" ] && [ -f "$T/home/.claude/settings.json" ] || { ko "$FUNCNAME" "link/apply did not run"; return; }
  grep -q -- "--pre-commit --staged" "$T/gitleaks.log" && grep -q -- "--log-opts" "$T/gitleaks.log" || { ko "$FUNCNAME" "gitleaks not run for both: $(cat "$T/gitleaks.log")"; return; }
  ok "$FUNCNAME"
}

sync_stops_when_gitleaks_finds_something() {
  local out rc before
  make_repos
  echo "agent memory" > "$T/home/tools/claude-personal/rules/agent-note.md"
  before="$(hub_heads)"
  out="$(FAKE_LEAKS=1 cc_sync --yes 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$(hub_heads)" ] || { ko "$FUNCNAME" "pushed despite gitleaks"; return; }
  [ "$(tgit -C "$T/home/tools/claude-personal" log --oneline | wc -l | tr -d ' ')" = "1" ] || { ko "$FUNCNAME" "committed despite gitleaks"; return; }
  [ -f "$T/home/tools/claude-personal/rules/agent-note.md" ] || { ko "$FUNCNAME" "the change was lost"; return; }
  [ -z "$(tgit -C "$T/home/tools/claude-personal" diff --cached --name-only)" ] || { ko "$FUNCNAME" "left staged"; return; }
  ok "$FUNCNAME"
}

sync_requires_clean_working_copies() {
  local out rc before
  make_repos
  echo "half done" > "$T/home/dev/claude-config/global/WIP.md"
  before="$(hub_heads)"
  out="$(cc_sync --yes 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  [ "$before" = "$(hub_heads)" ] || { ko "$FUNCNAME" "pushed with a dirty working copy"; return; }
  expect_has "$FUNCNAME" "dev/claude-config" "$out" || return
  ok "$FUNCNAME"
}

sync_stops_when_a_live_clone_cannot_fast_forward() {
  local out rc
  make_repos
  echo "local only" > "$T/home/tools/claude-config/global/LOCAL.md"
  tgit -C "$T/home/tools/claude-config" add -A && tgit -C "$T/home/tools/claude-config" commit -qm "live-only commit"
  dev_commit claude-config global/NEW.md "new general file"
  out="$(cc_sync --yes 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 1 "$rc" "$out" || return
  expect_has "$FUNCNAME" "fast-forward" "$out" || return
  [ ! -L "$T/home/.claude/CLAUDE.md" ] || { ko "$FUNCNAME" "linked after a failed pull"; return; }
  ok "$FUNCNAME"
}

sync_refuses_a_working_copy_that_diverged_from_the_hub() {
  local out rc before
  make_repos
  tgit clone -q "$T/hub/claude-config.git" "$T/other"
  echo "from elsewhere" > "$T/other/global/OTHER.md"
  tgit -C "$T/other" add -A && tgit -C "$T/other" commit -qm "other machine" && tgit -C "$T/other" push -q
  dev_commit claude-config global/NEW.md "new general file"
  before="$(hub_heads)"
  out="$(cc_sync --yes 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  expect_has "$FUNCNAME" "diverged" "$out" || return
  [ "$before" = "$(hub_heads)" ] || { ko "$FUNCNAME" "pushed a diverged branch"; return; }
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- install

cc_install() {  # runs the tool from outside the temp HOME, as on a fresh machine
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 HOME="$T/home" /bin/bash "$ROOT/bin/claude-config" install \
    --config-remote "$T/hub/claude-config.git" --personal-remote "$T/hub/claude-personal.git" "$@" < /dev/null
}

install_clones_both_repos_twice_then_links_and_applies() {
  local out rc
  make_repos
  rm -rf "$T/home/dev" "$T/home/tools"
  out="$(cc_install 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  for d in dev/claude-config dev/claude-personal tools/claude-config tools/claude-personal; do
    [ -d "$T/home/$d/.git" ] || { ko "$FUNCNAME" "missing clone $d"; return; }
  done
  [ "$(readlink "$T/home/.claude/CLAUDE.md")" = "$T/home/tools/claude-config/global/CLAUDE.md" ] || { ko "$FUNCNAME" "base link wrong"; return; }
  [ "$(readlink "$T/home/.claude/rules/personal.md")" = "$T/home/tools/claude-personal/rules/personal.md" ] || { ko "$FUNCNAME" "personal link wrong"; return; }
  [ "$(jq -r .theme "$T/home/.claude/settings.json")" = "dark" ] || { ko "$FUNCNAME" "settings not applied"; return; }
  out="$(cc_install 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 0 "$rc" "$out" || return
  ok "$FUNCNAME"
}

install_refuses_a_directory_that_is_not_the_expected_clone() {
  local out rc
  make_repos
  rm -rf "$T/home/dev/claude-config"
  mkdir -p "$T/home/dev/claude-config" && echo "something else" > "$T/home/dev/claude-config/file"
  out="$(cc_install 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 3 "$rc" "$out" || return
  expect_has "$FUNCNAME" "dev/claude-config" "$out" || return
  [ "$(cat "$T/home/dev/claude-config/file")" = "something else" ] || { ko "$FUNCNAME" "existing dir was touched"; return; }
  ok "$FUNCNAME"
}

install_without_remotes_is_a_usage_error() {
  local out rc
  out="$(GIT_CONFIG_GLOBAL=/dev/null HOME="$T/home" env -u CLAUDE_CONFIG_REMOTE_CONFIG -u CLAUDE_CONFIG_REMOTE_PERSONAL /bin/bash "$ROOT/bin/claude-config" install < /dev/null 2>&1)"; rc=$?
  expect_rc "$FUNCNAME" 2 "$rc" "$out" || return
  expect_has "$FUNCNAME" "--config-remote" "$out" || return
  [ ! -e "$T/home/dev/claude-config/.git" ] || { ko "$FUNCNAME" "cloned without a remote"; return; }
  ok "$FUNCNAME"
}

# ---------------------------------------------------------------- runner

CASES="
status_on_empty_home_reports_missing
status_reports_replaced_and_foreign
status_reports_missing_source
manifest_with_bad_line_is_rejected
manifest_target_outside_allowed_dirs_is_rejected
unknown_command_is_usage_error
link_on_empty_home_creates_symlinks_into_repo
link_twice_changes_nothing
link_backs_up_identical_file_outside_claude_dir
link_refuses_when_existing_content_differs
link_refuses_foreign_symlink
link_with_missing_source_is_manifest_error
link_dry_run_writes_nothing
file_created_in_linked_memory_dir_lands_in_repo
apply_on_clean_home_merges_base_and_machine
apply_twice_is_a_noop_without_a_backup
apply_writes_repo_change_and_keeps_previous_file_as_backup
apply_refuses_when_live_has_a_deny_the_repo_lacks
apply_refuses_when_repo_drops_a_deny
apply_discard_live_never_overrides_a_deny_loss
apply_refuses_when_live_changed_outside_the_tool
apply_first_run_refuses_an_unknown_live_file
apply_first_run_adopts_an_equivalent_live_file_untouched
apply_refuses_symlinked_settings
apply_with_invalid_base_json_is_an_error
apply_dry_run_writes_nothing
status_is_clean_after_link_and_apply
status_tells_drift_from_pending_and_deny_loss
diff_is_clean_when_everything_is_in_sync
diff_shows_settings_and_file_changes_without_writing
symlink_replaced_by_file_is_detected_and_pulled_back
pull_without_relink_leaves_the_live_file_alone
pull_brings_live_settings_changes_into_base_then_apply_passes
pull_brings_an_extra_live_deny_into_base
pull_keeps_machine_keys_out_of_base
pull_refuses_to_drop_a_deny_from_base
pull_does_not_revert_a_pending_repo_change
pull_refuses_when_repo_and_live_both_changed
pull_dry_run_writes_nothing
unlink_replaces_symlinks_with_identical_copies
unlink_dry_run_writes_nothing
tool_writes_only_to_its_three_locations
status_warns_about_a_link_dropped_from_the_manifest
status_warns_about_a_dangling_link_into_the_repo
status_ignores_links_that_point_elsewhere
links_from_a_working_copy_still_point_into_the_live_clone
live_root_inside_dev_is_refused
personal_manifest_links_into_the_personal_live_clone
apply_merges_base_personal_and_machine
hooks_from_every_layer_are_united_without_duplicates
apply_refuses_when_the_personal_layer_drops_a_live_deny
pull_with_a_personal_repo_writes_a_minimal_personal_overlay
pull_keeps_machine_values_out_of_the_personal_layer
pull_refuses_to_drop_a_personal_deny
pull_refuses_a_change_the_personal_layer_cannot_express
personal_dirty_hook_speaks_only_when_the_personal_clone_has_changes
sync_without_yes_shows_the_plan_and_changes_nothing
sync_yes_pushes_live_and_dev_changes_then_pulls_links_and_applies
sync_stops_when_gitleaks_finds_something
sync_requires_clean_working_copies
sync_stops_when_a_live_clone_cannot_fast_forward
sync_refuses_a_working_copy_that_diverged_from_the_hub
install_clones_both_repos_twice_then_links_and_applies
install_refuses_a_directory_that_is_not_the_expected_clone
install_without_remotes_is_a_usage_error
"

real_home_sig() { ls -ld "$REAL_HOME/.local/state/claude-config" "$REAL_HOME/.config/claude-kit" "$REAL_HOME/.claude/settings.json" 2>&1; }
REAL_BEFORE="$(real_home_sig)"

for c in $CASES; do run_case "$c"; done

if [ "$REAL_BEFORE" = "$(real_home_sig)" ]; then ok "real_home_untouched"; else ko "real_home_untouched" "the real HOME changed during the run"; fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
