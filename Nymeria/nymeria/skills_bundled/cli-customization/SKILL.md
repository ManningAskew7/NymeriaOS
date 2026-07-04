---
name: cli-customization
description: Read and reconfigure the user's Nymeria terminal CLI status bars
  (segment order, a second under-prompt bar, static text labels, and script
  segments) on their behalf. Load this when the user asks you to change,
  simplify, or extend what their CLI status line shows.
metadata:
  nymeria:
    required_tools:
      - cli_statusbar_get
      - cli_statusbar_set
    tool_ttl: 2h
---

# CLI Customization

The Nymeria terminal CLI (the Rich REPL) renders a status bar above the
composer and can render a second, hidden-by-default bar below it. Both are
user-configurable, and this kit lets you make those changes for the user:
the same edits they could make themselves with the local `/statusbar`
command, applied live in every CLI session they have open and persisted in
their local `cli.json`.

Requirement: the user must have a CLI session connected. The tools error
out after a short timeout otherwise; that error means "no CLI is open",
not a fault in your call. These tools do not affect the desktop or mobile
apps.

## The layout model

Two bars, each an ordered list of segment refs:

- `top`: the main bar above the composer. When unset it follows the
  built-in default order (which gains new segments across upgrades);
  setting it pins exactly the listed segments.
- `under`: a second bar below the composer. Empty means hidden.

A segment ref is one of:

- a built-in key: `brand`, `activity`, `notice`, `connection`, `model`,
  `fast`, `reasoning`, `thread`, `context`, `tps`, `queued`, `cwd`
- `text:<literal>`: a static label (e.g. `text:PROD`)
- `script:<command>`: a script segment run periodically on the user's own
  machine (JSON snapshot on stdin, first stdout line shown). You can READ
  these via `cli_statusbar_get`, but you can NOT push them: both the tool
  and the CLI reject `script:` refs from this channel, because they
  execute code on the user's machine. If a script segment is the right
  answer, give the user the exact local command to run themselves, e.g.
  `/statusbar set under "script:git branch --show-current"`.

## Workflow

1. `cli_statusbar_get` first: see the current layout and the built-in keys
   before changing anything.
2. `cli_statusbar_set` with the bar name and the FULL ordered segment
   list you want (it replaces the bar, it does not append). An empty list
   resets: `top` back to the default order, `under` back to hidden.
3. Read the returned layout back to the user so they know what changed.

Examples:

- "put tokens per second on my status bar" with a pinned minimal top bar:
  `cli_statusbar_set(bar="top", segments=["model", "context", "tps"])`
- "show a static label under my prompt":
  `cli_statusbar_set(bar="under", segments=["text:PROD", "context"])`
- "show the git branch under my prompt": script segments are user-installed
  only, so reply with the command for them to run:
  `/statusbar set under "script:git branch --show-current"`
- "put everything back": `cli_statusbar_set(bar="top", segments=[])` and
  `cli_statusbar_set(bar="under", segments=[])`

## Cautions

- Prefer keeping `activity` and `notice` in the top bar: they carry busy
  state and error notices. Warn the user when asked to remove them.
- When suggesting a `script:` command for the user to install, keep it
  fast and read-only, and never suggest commands that write, delete, or
  send data.
- Changes persist across CLI restarts. If the user just wants a look, say
  how to revert (`/statusbar reset`, or an empty `segments` list).
