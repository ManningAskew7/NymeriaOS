# cli/

Interactive terminal CLI for Nymeria.

## Start here

`app.py`  -  CLI application entry point and main loop.

## Contents

- `app.py`  -  main CLI app
- `capabilities.py`  -  terminal detection and renderer policy: explicit `--renderer` beats the `TERM`/CI heuristics, tty facts beat both; a Windows console with no `TERM` is asked directly (VT enable); `scroll_region_safe` gates the pinned footer (`WT_SESSION` on Windows)
- `follow_footer.py`  -  pinned-footer / scroll-region engine behind the Rich REPL
- `commands/`  -  slash command handlers (context, memory, model, system, threads, tools; the todo family is backend-owned since #143)
- `rendering/`  -  terminal output formatting
- `transport/`  -  communication layer
- `state/`  -  CLI session state
- `input.py`, `completion.py`  -  input handling and tab completion
- `autonomous.py`  -  autonomous mode support
- `theme.py`  -  terminal color theme
- `header.py`  -  CLI header display
- `lifecycle.py`  -  startup/shutdown
