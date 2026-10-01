#!/bin/bash
# Integration checks with the real `claude` binary against a local fake Messages API.
# No API key, no spend, temporary HOME; the real HOME is not used. Not part of tests/run.sh.
#
# STATUS: written 2026-09-30 by an agent session that was not allowed to launch a nested `claude`.
# First run by the owner on 2026-10-01, Claude Code 2.1.286:
#   A: CLAUDE.md, skill and rule found in both the copy and the link run (symlinks work).
#   B: project-level enabledPlugins:false switches off a user-level plugin.
# Re-run after Claude Code updates that touch config loading, and record the version.
#
# What it answers:
#   A. Does Claude Code load a CLAUDE.md, a skill and a rule that are symlinks into another directory?
#      (2.1.284 changed how rules symlinked from outside a project are treated; unknown for ~/.claude/rules.)
#      If the rule marker is missing in the "link" run but present in the "copy" run, rules must be
#      copied by `apply` instead of linked.
#   B. Does `enabledPlugins: false` in a project's .claude/settings.json switch off a plugin that is
#      enabled at user level? Decides whether profiles can turn plugins off or only add them.
#
# Usage: FAKE_UPSTREAM=/path/to/fake_anthropic.py tests/integration.sh
set -u

FAKE="${FAKE_UPSTREAM:-}"
[ -n "$FAKE" ] && [ -f "$FAKE" ] || { echo "set FAKE_UPSTREAM to the path of a fake Messages API server (fake_anthropic.py)" >&2; exit 2; }
command -v claude >/dev/null || { echo "claude not on PATH" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 not on PATH" >&2; exit 2; }

W="$(mktemp -d)"
W="$(cd "$W" && pwd -P)"
trap 'rm -rf "$W"' EXIT
echo "claude version: $(claude --version)"
echo "work dir: $W"

# run_claude <home> <project dir> <out dir>: one `claude -p hello`; request bodies land in <out dir>/bodies.
run_claude() {
  local home="$1" proj="$2" out="$3" port sp
  mkdir -p "$out"
  port="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
  python3 "$FAKE" "$port" "$out/bodies" >/dev/null 2>&1 &
  sp=$!
  sleep 0.8
  (cd "$proj" && env -i HOME="$home" PATH="$PATH" TERM=dumb \
    ANTHROPIC_BASE_URL="http://127.0.0.1:$port" ANTHROPIC_API_KEY=sk-ant-dummy \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    claude -p "hello" --output-format json < /dev/null > "$out/stdout.txt" 2> "$out/stderr.txt")
  kill "$sp" 2>/dev/null
}

# seen <out dir> <marker>: "found" or "absent"
seen() { if grep -rqs -- "$2" "$1/bodies"; then echo "found"; else echo "absent"; fi; }

new_project() { mkdir -p "$1" && git -C "$1" init -q; }

# ---------------------------------------------------------------- A. symlinked CLAUDE.md, skill, rule

SRC="$W/config-repo"
mkdir -p "$SRC/skills/linked-skill" "$SRC/rules"
echo "MARKER-CLAUDE-MD-7f3a: always mention this marker." > "$SRC/CLAUDE.md"
printf -- '---\nname: linked-skill\ndescription: MARKER-SKILL-91bc use when the user asks about linked skills\n---\nBody.\n' > "$SRC/skills/linked-skill/SKILL.md"
echo "MARKER-RULE-55de: a rule without paths frontmatter." > "$SRC/rules/linked-rule.md"

for variant in copy link; do
  home="$W/A-$variant/home"
  mkdir -p "$home/.claude/skills" "$home/.claude/rules"
  if [ "$variant" = "copy" ]; then
    cp "$SRC/CLAUDE.md" "$home/.claude/CLAUDE.md"
    cp -R "$SRC/skills/linked-skill" "$home/.claude/skills/linked-skill"
    cp "$SRC/rules/linked-rule.md" "$home/.claude/rules/linked-rule.md"
  else
    ln -s "$SRC/CLAUDE.md" "$home/.claude/CLAUDE.md"
    ln -s "$SRC/skills/linked-skill" "$home/.claude/skills/linked-skill"
    ln -s "$SRC/rules/linked-rule.md" "$home/.claude/rules/linked-rule.md"
  fi
  new_project "$W/A-$variant/proj"
  run_claude "$home" "$W/A-$variant/proj" "$W/A-$variant/out"
done

echo
echo "A. symlinked config (copy = control, link = what claude-config does)"
printf '%-12s %-8s %-8s\n' "" copy link
for m in MARKER-CLAUDE-MD-7f3a MARKER-SKILL-91bc MARKER-RULE-55de; do
  printf '%-12s %-8s %-8s\n' "${m#MARKER-}" "$(seen "$W/A-copy/out" "$m")" "$(seen "$W/A-link/out" "$m")"
done
echo "Read it as: 'found/found' = symlinks work. 'found/absent' = that item must be copied, not linked."
echo "'absent/absent' = the check itself is broken (see $W/A-copy/out/stderr.txt before the work dir is removed)."

# ---------------------------------------------------------------- B. project-level enabledPlugins: false

MP="$W/marketplace"
mkdir -p "$MP/.claude-plugin" "$MP/probe/.claude-plugin" "$MP/probe/skills/probe-skill"
cat > "$MP/.claude-plugin/marketplace.json" <<'JSON'
{ "name": "local-test", "owner": { "name": "claude-config tests" },
  "plugins": [ { "name": "probe", "source": "./probe", "description": "probe plugin" } ] }
JSON
echo '{ "name": "probe", "version": "0.0.1", "description": "probe plugin" }' > "$MP/probe/.claude-plugin/plugin.json"
printf -- '---\nname: probe-skill\ndescription: MARKER-PLUGIN-c4e2 use when the user asks about the probe plugin\n---\nBody.\n' > "$MP/probe/skills/probe-skill/SKILL.md"

home="$W/B/home"
mkdir -p "$home"
cc_plugin() { env -i HOME="$home" PATH="$PATH" TERM=dumb claude plugin "$@"; }
if cc_plugin marketplace add "$MP" > "$W/B-setup.txt" 2>&1 && cc_plugin install probe@local-test >> "$W/B-setup.txt" 2>&1; then
  new_project "$W/B/proj-default"
  run_claude "$home" "$W/B/proj-default" "$W/B/out-default"
  new_project "$W/B/proj-off"
  mkdir -p "$W/B/proj-off/.claude"
  echo '{ "enabledPlugins": { "probe@local-test": false } }' > "$W/B/proj-off/.claude/settings.json"
  run_claude "$home" "$W/B/proj-off" "$W/B/out-off"
  echo
  echo "B. plugin enabled at user level"
  echo "  project without settings:          $(seen "$W/B/out-default" MARKER-PLUGIN-c4e2)"
  echo "  project with enabledPlugins false: $(seen "$W/B/out-off" MARKER-PLUGIN-c4e2)"
  echo "Read it as: 'found' then 'absent' = a project can switch a user-level plugin off."
  echo "'found' then 'found' = it cannot; profiles can only add plugins. 'absent' first = the setup failed."
else
  echo
  echo "B. skipped: could not install the probe plugin from a local marketplace:"
  sed 's/^/  /' "$W/B-setup.txt"
fi
