#!/usr/bin/env bash
# Status line for Claude Code (settings.json -> statusLine.command). Reads the session JSON on stdin.
#   line 1: 📁 dir │ 🌿 branch +staged !modified ?untracked │ model · effort <level>
#   line 2: context bar + % │ prompt-cache hit % (COLD, last miss cause) │ session $ │ 5h limit % │ duration
# Missing data is a gray "-". Based on claude-code-kit global/statusline.sh; colours, bar and separators
# follow a statusline-builder layout.
# Why: context % catches bloat before compaction; the prompt_cache object (v2.1.251+) and last_miss_cause
# (v2.1.260+) show cache misses (model/effort/tool changes re-bill the whole context); cost is a client-side
# list-price estimate. Source: code.claude.com/docs/en/statusline.
# Context colours: used_percentage is counted against context_window_size (the model's window), while
# compaction starts at autoCompactWindow. Yellow at 60% and red at 80% of autoCompactWindow, expressed as a
# share of the window (400k of 1M: 24% / 32%). Only user settings are read for autoCompactWindow.
# Runs locally: costs zero model tokens. Keep it fast: one jq and one git process (debounced 300 ms).
in=$(cat)

settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
[ -r "$settings" ] || settings=/dev/null

US=$'\037'
fields=$(jq -r --rawfile st "$settings" '
  def int: if type == "number" then floor | tostring else "" end;
  [ (.model.display_name // "Claude"),
    (.effort.level // ""),
    (.workspace.current_dir // .cwd // ""),
    (.context_window.used_percentage | int),
    (.context_window.context_window_size | int),
    (($st | try fromjson catch {}) | .autoCompactWindow? | int),
    (if .prompt_cache then (.prompt_cache.hit_ratio // 0) * 100 | int else "" end),
    (if .prompt_cache and (.prompt_cache.warm | not) then "1" else "" end),
    ((.prompt_cache.last_miss_cause.causes? // []) | join(",")),
    (if .cost.total_cost_usd then .cost.total_cost_usd * 100 | round | int else "" end),
    (.rate_limits.five_hour.used_percentage | int),
    (.cost.total_duration_ms | int)
  ] | join("\u001f")' <<<"$in" 2>/dev/null) || { printf '%s\n' "[claude]"; exit 0; }

IFS=$US read -r model effort cwd ctx size acw hit cold causes cents five dur <<<"$fields"

CSI=$'\033['
RST="${CSI}0m"
GREEN=78 YELLOW=220 RED=196 GRAY=240
out=""
paint() { out="$out${CSI}38;5;${1}m$2$RST"; }
dash() { paint "$GRAY" -; }
sep() { out="$out ${CSI}2m${CSI}38;5;${GRAY}m│$RST "; }
# level <value> <warn> <crit>: sets $color
level() {
  if [ "$1" -ge "$3" ]; then color=$RED; elif [ "$1" -ge "$2" ]; then color=$YELLOW; else color=$GREEN; fi
}

# ---- line 1
out="📁 "
if [ -n "$cwd" ]; then
  case "$cwd" in
    "$HOME" | "$HOME"/*) paint 111 "~${cwd#"$HOME"}" ;;
    *) paint 111 "$cwd" ;;
  esac
else
  dash
fi

sep
out="${out}🌿 "
git_info=""
[ -n "$cwd" ] && git_info=$(git -C "$cwd" --no-optional-locks status --porcelain=v2 --branch 2>/dev/null | awk '
  /^# branch.head / { b = $3 }
  /^[12u] / { if (substr($2, 1, 1) != ".") s++; if (substr($2, 2, 1) != ".") m++ }
  /^\? / { u++ }
  END { if (b != "") printf "%s %d %d %d", b, s, m, u }')
if [ -n "$git_info" ]; then
  read -r branch staged modified untracked <<<"$git_info"
  paint 117 "$branch"
  marks=""
  [ "$staged" -gt 0 ] && marks="+$staged"
  [ "$modified" -gt 0 ] && marks="${marks:+$marks }!$modified"
  [ "$untracked" -gt 0 ] && marks="${marks:+$marks }?$untracked"
  [ -n "$marks" ] && { out="$out "; paint 141 "$marks"; }
else
  dash
fi

sep
out="$out${CSI}38;5;111;1m$model$RST · effort "
if [ -n "$effort" ]; then paint 141 "$effort"; else dash; fi
line1=$out

# ---- line 2
out=""
if [ -n "$ctx" ]; then
  warn=60 crit=80
  if [ -n "$size" ] && [ "$size" -gt 0 ]; then
    window=${acw:-$size}
    [ "$window" -gt "$size" ] && window=$size
    warn=$((60 * window / size))
    crit=$((80 * window / size))
  fi
  level "$ctx" "$warn" "$crit"
  filled=$((ctx * 10 / 100))
  [ "$filled" -gt 10 ] && filled=10
  bar="" i=0
  while [ "$i" -lt "$filled" ]; do
    if [ "$i" -lt 2 ]; then bar="${bar}█"; else bar="${bar}⣿"; fi
    i=$((i + 1))
  done
  out="${CSI}38;5;${color}m$bar"
  ctx_text="$ctx%"
else
  filled=0
  color=$GRAY
  ctx_text="-"
fi
empty=""
i=$filled
while [ "$i" -lt 10 ]; do empty="${empty}⣀"; i=$((i + 1)); done
out="$out${CSI}38;5;${GRAY}m$empty$RST "
paint "$color" "$ctx_text"

sep
out="${out}cache "
if [ -n "$hit" ]; then
  paint 117 "$hit%"
  [ -n "$cold" ] && { out="$out "; paint "$RED" COLD; }
  [ -n "$causes" ] && { out="$out "; paint "$YELLOW" "miss:$causes"; }
else
  dash
fi

sep
if [ -n "$cents" ]; then
  if [ "$cents" -gt 1000 ]; then color=$RED; elif [ "$cents" -gt 200 ]; then color=$YELLOW; else color=$GREEN; fi
  printf -v money '$%d.%02d' $((cents / 100)) $((cents % 100))
  paint "$color" "$money"
else
  out="$out\$"
  dash
fi

sep
out="${out}5h "
if [ -n "$five" ]; then
  level "$five" 50 80
  paint "$color" "$five%"
else
  dash
fi

sep
if [ -n "$dur" ]; then
  s=$((dur / 1000))
  if [ "$s" -ge 3600 ]; then t="$((s / 3600))h $((s % 3600 / 60))m"
  elif [ "$s" -ge 60 ]; then t="$((s / 60))m $((s % 60))s"
  else t="${s}s"
  fi
  paint 111 "$t"
else
  dash
fi

printf '%s\n%s\n' "$line1" "$out"
