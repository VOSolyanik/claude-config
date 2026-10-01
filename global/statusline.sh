#!/bin/bash
# Claude Code statusLine: model, cwd, git branch, remaining context %
input=$(cat)

model=$(echo "$input" | jq -r '.model.display_name // "Claude"')

cwd=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // empty')
dir=$(echo "$cwd" | sed "s|^$HOME|~|")

branch=""
if git -C "$cwd" --no-optional-locks rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git -C "$cwd" --no-optional-locks branch --show-current 2>/dev/null)
  [ -n "$branch" ] && branch=" ($branch)"
fi

remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')
ctx=""
[ -n "$remaining" ] && ctx=" | $(printf '%.0f' "$remaining")% left"

printf "%s | %s%s%s" "$model" "$dir" "$branch" "$ctx"
