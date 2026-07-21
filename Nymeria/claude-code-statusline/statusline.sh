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
tpath=$(j '.transcript_path // ""')
dir=$(j '.workspace.current_dir // .cwd // ""')
dir=${dir//\\//}  # Windows paths: normalize backslashes so the basename split works
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
      # All three fds must detach at the subshell level: on Windows the native
      # git child otherwise inherits the statusline stdout pipe handle, the
      # reader never sees EOF, and the whole line goes blank. The credential
      # override keeps a promptless context from hanging on a GUI cred dialog.
      ( GIT_TERMINAL_PROMPT=0 git -c credential.interactive=false fetch --quiet & ) >/dev/null 2>&1 0</dev/null
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

# session credit estimate (metered model only): sums this transcript's
# credit-billed (fable) assistant turns at list rates ($10 in / $50 out,
# cache write $12.50 5m / $20 1h, cache read $1, per Mtok), deduped by
# message id (one API turn spans several JSONL lines, last line wins).
# The model filter is what excludes subagent turns (they are pinned to
# in-plan models via CLAUDE_CODE_SUBAGENT_MODEL; current versions also
# keep them out of this file entirely), so unlike cost.total_cost_usd
# this counts only usage-credit traffic. The transcript format is
# internal to Claude Code and can change between versions; if parsing
# fails the segment silently disappears rather than erroring.
if [ "$model_c" = "$orange" ] && [ -r "$tpath" ]; then
  cr_usd=$(grep -a '"type":"assistant"' "$tpath" 2>/dev/null \
    | jq -r 'select((.message.model // "") | ascii_downcase | contains("fable"))
        | .message.usage as $u
        | ($u.cache_creation.ephemeral_1h_input_tokens // 0) as $c1
        | ($u.cache_creation.ephemeral_5m_input_tokens // ($u.cache_creation_input_tokens // 0)) as $c5
        | "\(.message.id // "x") \((($u.input_tokens // 0)*10) + (($u.output_tokens // 0)*50) + ($c5*12.5) + ($c1*20) + (($u.cache_read_input_tokens // 0)*1))"' 2>/dev/null \
    | awk '{v[$1]=$2} END{s=0; for(k in v) s+=v[k]; if (s>=10000) printf "%.2f", s/1e6}')
  [ -n "$cr_usd" ] && out="${out}${sep}${grey}cr ${reset}${orange}~\$${cr_usd}${reset}"
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
