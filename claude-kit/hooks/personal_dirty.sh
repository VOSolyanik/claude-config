#!/bin/bash
# SessionStart: one sentence when the live clone of claude-personal has uncommitted changes
# (memory or writing the agent added through the symlinks), so they get reviewed and pushed.
# Silent otherwise; always exits 0. Only a `git status`, no network.
dir="${CLAUDE_CONFIG_LIVE_ROOT:-$HOME/tools}/claude-personal"
[ -d "$dir/.git" ] || exit 0
if [ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ]; then
  echo "claude-personal has uncommitted changes (memory or writing from the agent): ask the user to run \`claude-config sync\` to review and push them."
fi
exit 0
