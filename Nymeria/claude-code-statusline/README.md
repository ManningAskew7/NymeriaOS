# Claude Code status line

A single-line, colorized status line for Claude Code, themed to match Nymeria's
palette. This package holds the script (`statusline.sh`). The full documentation
(segments, install, customizing) lives in
`Nymeria/docs/private/ClaudeCode/statusline.md`.

Quick install:

```bash
cp Nymeria/claude-code-statusline/statusline.sh ~/.claude/statusline.sh
chmod +x ~/.claude/statusline.sh
```

Then register it under the `statusLine` key in `~/.claude/settings.json` and
start a new session. See `statusline.md` for the segment reference and
customization.
