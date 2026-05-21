# cli/

Interactive terminal CLI for Nymeria.

## Start here

`app.py`  -  CLI application entry point and main loop.

## Contents

- `app.py`  -  main CLI app
- `commands/`  -  slash command handlers (context, memory, model, system, threads, todos, tools)
- `rendering/`  -  terminal output formatting
- `transport/`  -  communication layer
- `state/`  -  CLI session state
- `input.py`, `completion.py`  -  input handling and tab completion
- `autonomous.py`  -  autonomous mode support
- `theme.py`  -  terminal color theme
- `header.py`  -  CLI header display
- `lifecycle.py`  -  startup/shutdown
