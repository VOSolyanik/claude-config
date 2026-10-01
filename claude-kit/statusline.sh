#!/usr/bin/env bash
# Status line for Claude Code (settings.json -> statusLine.command). Reads the session JSON on stdin.
# Shows: cwd (git branch) | model | effort | context % | prompt-cache hit ratio + warm/cold + last miss cause
#        | session $ | 5h limit %.
# Based on claude-code-kit global/statusline.sh; cwd and branch added in front.
# Why: context % catches bloat before compaction; the prompt_cache object (v2.1.251+) and last_miss_cause
# (v2.1.260+) show cache misses (model/effort/tool changes re-bill the whole context); cost is a client-side
# list-price estimate. Source: code.claude.com/docs/en/statusline.
# Runs locally: costs zero model tokens. Keep it fast (debounced 300 ms; slow scripts get cancelled).
in=$(cat)

kit=$(jq -r '
  def pct(x): if x == null then "-" else ((x|floor|tostring) + "%") end;
  [ "[" + (.model.display_name // "?") + "]",
    (if .effort.level then "effort " + .effort.level else empty end),
    "ctx " + pct(.context_window.used_percentage),
    (if .prompt_cache then
        "cache " + pct((.prompt_cache.hit_ratio // 0) * 100)
        + (if .prompt_cache.warm then " warm" else " COLD" end)
        + (if .prompt_cache.ttl then "/" + .prompt_cache.ttl else "" end)
        + (if (.prompt_cache.last_miss_cause.causes // []) | length > 0
           then " miss:" + (.prompt_cache.last_miss_cause.causes | join(",")) else "" end)
     else empty end),
    (if .cost.total_cost_usd then "$" + ((.cost.total_cost_usd * 100 | round) / 100 | tostring) else empty end),
    (if .rate_limits.five_hour.used_percentage then "5h " + pct(.rate_limits.five_hour.used_percentage) else empty end)
  ] | join(" | ")' <<<"$in" 2>/dev/null) || { printf '%s' "[claude]"; exit 0; }

cwd=$(jq -r '.workspace.current_dir // .cwd // empty' <<<"$in" 2>/dev/null)
where=""
if [ -n "$cwd" ]; then
  case "$cwd" in
    "$HOME" | "$HOME"/*) where="~${cwd#"$HOME"}" ;;
    *) where=$cwd ;;
  esac
  branch=$(git -C "$cwd" --no-optional-locks branch --show-current 2>/dev/null)
  [ -n "$branch" ] && where="$where ($branch)"
  where="$where | "
fi

printf '%s%s' "$where" "$kit"
