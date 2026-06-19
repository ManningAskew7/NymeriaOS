# Claude Code status line

A single-line, colorized status line for Claude Code, themed to match Nymeria's
palette. Claude Code pipes a JSON blob on stdin; the script reads it with `jq`
and prints one formatted line.

This is a machine-local Claude Code operator artifact (like the `~/.claude/`
hooks), tracked here so it is versioned and portable across dev machines. It is
not part of the Nymeria application and nothing in the backend depends on it.

## What it shows

Left to right, separated by ` | `:

| Segment | Example | Notes |
| --- | --- | --- |
| Model + effort | `✦ Opus 4.8/mx` | Effort code: `l/m/h/xh/mx` (low..max); omitted if unset |
| Directory | `NymeriaOS` | Basename of the workspace dir |
| Git | `🌿 main *3 ↓0` | Branch, uncommitted tracked count (`*N`), commits behind upstream (`↓N`, red when behind) |
| Context | `137k/200k ctx` | Input tokens used / context window; accent < 200k, orange ≥ 200k, red ≥ 400k |
| Code churn | `+412/-89` | Lines added / removed this session (shown only when nonzero) |
| Subscription usage | `5h 42% ·11pm 7d 18%` | Max-plan 5-hour and 7-day pools; reset clock in the configured timezone; orange ≥ 75%, red ≥ 90% |

The commits-behind segment kicks off a throttled (>= 180s apart), non-blocking
`git fetch` in the background, so the `↓N` count can be a few minutes stale.

## Install

1. Copy (or symlink) the script into your Claude Code config dir:

   ```bash
   cp Nymeria/claude-code-statusline/statusline.sh ~/.claude/statusline.sh
   chmod +x ~/.claude/statusline.sh
   # or, to track this copy: ln -sf "$PWD/Nymeria/claude-code-statusline/statusline.sh" ~/.claude/statusline.sh
   ```

2. Register it in `~/.claude/settings.json`:

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "~/.claude/statusline.sh",
       "padding": 0,
       "refreshInterval": 60
     }
   }
   ```

3. Requires `jq` and `git` on `PATH`. Start a new Claude Code session (or run
   `/statusline` to reconfigure) to pick it up.

## Customizing

- **Colors** live in the `# ANSI palette` block near the top (24-bit truecolor
  hex values). The defaults match Nymeria's accent periwinkle `#bbddfb`.
- **Timezone** for the reset clock is hardcoded as `Australia/Sydney` in the
  subscription-usage block (auto-tracks AEST/AEDT); change both `TZ=` values to
  relocate.
- **Thresholds** for context and usage coloring are the numeric comparisons in
  the context-color and `pct_seg` blocks.
