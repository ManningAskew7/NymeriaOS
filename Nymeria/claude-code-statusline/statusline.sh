#!/usr/bin/env bash
# Claude Code status line - single line, color.
#
# Segments: model/effort | cwd | git branch + uncommitted count + commits-behind |
#           context as tokens (used/window) | code churn | credit estimate
#           (metered models only) | subscription usage
#
# Palette (Nymeria theme): accent periwinkle #bbddfb = live gauge numbers;
# white = model name (orange when the model bills usage credits, e.g. Fable);
# lavender = location/identity (dir, branch); grey = chrome (labels,
# separators, effort, reset time); green/red = churn; yellow/orange/red =
# threshold alerts only.
# Reads the JSON Claude Code pipes on stdin. Null-safe throughout.

input=$(cat)

j() { printf '%s' "$input" | jq -r "$1"; }

name=$(j '.model.display_name // "unknown"')
mid=$(j '.model.id // ""')
effort=$(j '.effort.level // empty')
cost_usd=$(j '.cost.total_cost_usd // 0')
dir=$(j '.workspace.current_dir // .cwd // ""')
used=$(j '.context_window.total_input_tokens // 0')
size=$(j '.context_window.context_window_size // 0')
added=$(j '.cost.total_lines_added // 0')
removed=$(j '.cost.total_lines_removed // 0')
rl5=$(j '.rate_limits.five_hour.used_percentage // empty')
rl7=$(j '.rate_limits.seven_day.used_percentage // empty')
rl5_reset=$(j '.rate_limits.five_hour.resets_at // empty')

# ANSI palette
accent=$'\033[38;2;187;221;251m'   # #bbddfb periwinkle - live gauge numbers
lavender=$'\033[38;2;216;200;255m' # #d8c8ff - location/identity (dir, branch)
grey=$'\033[38;2;138;147;163m'     # #8a93a3 - chrome (labels, separators)
white=$'\033[97m'
green=$'\033[32m'; yellow=$'\033[1;33m'; orange=$'\033[38;5;208m'; red=$'\033[31m'
reset=$'\033[0m'

# metered-model cue: Fable bills to usage credits, not the plan pools
model_c=$white
case "${mid,,} ${name,,}" in *fable*) model_c=$orange ;; esac

# 12345 -> 12k ; 1000000 -> 1.0M
humanize() {
  local n=$1
  if   [ "$n" -ge 1000000 ]; then printf '%d.%dM' $((n/1000000)) $(((n%1000000)/100000))
  elif [ "$n" -ge 1000 ];    then printf '%dk' $((n/1000))
  else printf '%d' "$n"; fi
}

# context color by absolute input tokens: <200k accent, >=200k orange, >=400k red
if   [ "$used" -ge 400000 ]; then ctx_c=$red
elif [ "$used" -ge 200000 ]; then ctx_c=$orange
else ctx_c=$accent; fi

sep="${grey} | ${reset}"

# short effort code next to the model
case "$effort" in
  low) eff=l ;; medium) eff=m ;; high) eff=h ;; xhigh) eff=xh ;; max) eff=mx ;; *) eff="" ;;
esac

out="${accent}✦ ${model_c}${name}${reset}"
[ -n "$eff" ] && out="${out}${grey}/${eff}${reset}"
[ -n "$dir" ] && out="${out}${sep}${lavender}${dir##*/}${reset}"

# git: branch + uncommitted(tracked) count + commits behind upstream
if gitdir=$(git rev-parse --absolute-git-dir 2>/dev/null); then
  branch=$(git branch --show-current 2>/dev/null)
  dirty=$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')
  gseg="${lavender}\U1F33F ${branch}${reset}"
  [ "$dirty" -gt 0 ] && gseg="${gseg} ${grey}*${dirty}${reset}"

  # commits behind upstream (origin/main when on main). Git only learns of new
  # remote commits via fetch, so kick off a throttled background fetch (>=180s
  # apart, non-blocking) and read the count from local refs - at most a few
  # minutes stale. Shows grey down0 when current, red downN when behind.
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
  if [ -n "$upstream" ]; then
    stamp="${gitdir}/.statusline_fetch"
    last=$(stat -c %Y "$stamp" 2>/dev/null || stat -f %m "$stamp" 2>/dev/null || echo 0)
    if [ "$(( $(date +%s) - last ))" -ge 180 ]; then
      : > "$stamp"
      ( GIT_TERMINAL_PROMPT=0 git fetch --quiet >/dev/null 2>&1 & )
    fi
    behind=$(git rev-list --count "HEAD..@{u}" 2>/dev/null)
    if [ -n "$behind" ] && [ "$behind" -gt 0 ]; then
      gseg="${gseg} ${red}↓${behind}${reset}"
    else
      gseg="${gseg} ${grey}↓0${reset}"
    fi
  fi

  out="${out}${sep}${gseg}"
fi

# context as tokens
if [ "$size" -gt 0 ]; then
  out="${out}${sep}${ctx_c}$(humanize "$used")/$(humanize "$size")${reset}${grey} ctx${reset}"
else
  out="${out}${sep}${ctx_c}$(humanize "$used")${reset}${grey} ctx${reset}"
fi

# code churn
if [ "$added" -gt 0 ] || [ "$removed" -gt 0 ]; then
  out="${out}${sep}${green}+${added}${reset}/${red}-${removed}${reset}"
fi

# session credit estimate (metered model only): Claude Code's whole-session
# cost at list rates; subagent turns bill the plan pools instead, so read it
# as a ceiling on credit burn, not an exact meter
if [ "$model_c" = "$orange" ] && awk -v c="$cost_usd" 'BEGIN{exit !(c>=0.01)}'; then
  out="${out}${sep}${grey}cr ${reset}${orange}~\$$(printf '%.2f' "$cost_usd")${reset}"
fi

# subscription usage pools (Max plan; absent until the first API response)
# color a rounded percentage by threshold, returns "NN%"
pct_seg() {
  local p; p=$(printf '%.0f' "$1")
  local c=$grey
  [ "$p" -ge 75 ] && c=$orange
  [ "$p" -ge 90 ] && c=$red
  printf '%s%s%%%s' "$c" "$p" "$reset"
}
if [ -n "$rl5" ] || [ -n "$rl7" ]; then
  budget=""
  if [ -n "$rl5" ]; then
    budget="${accent}5h ${reset}$(pct_seg "$rl5")"
    if [ -n "$rl5_reset" ]; then
      # reset clock in the user's zone (auto-tracks AEST/AEDT); GNU then BSD date
      reset_t=$(TZ='Australia/Sydney' date -d "@$rl5_reset" '+%-I:%M%p' 2>/dev/null \
             || TZ='Australia/Sydney' date -r "$rl5_reset" '+%l:%M%p' 2>/dev/null)
      reset_t=$(printf '%s' "$reset_t" | tr '[:upper:]' '[:lower:]' | tr -d ' ')
      reset_t=${reset_t/:00/}   # drop minutes when on the hour (11:00pm -> 11pm)
      [ -n "$reset_t" ] && budget="${budget}${accent} ·${reset_t}${reset}"
    fi
  fi
  [ -n "$rl7" ] && budget="${budget:+$budget }${accent}7d ${reset}$(pct_seg "$rl7")"
  out="${out}${sep}${budget}"
fi

printf '%b' "$out"
