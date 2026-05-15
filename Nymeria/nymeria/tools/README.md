# tools/

Agent tool implementations. Three categories: core (always loaded), optional (per-thread), and callable threads (dynamic).

## Start here

`__init__.py` — tool registry. Defines `ALL_TOOLS` (core) and `OPTIONAL_TOOLS` (per-thread enablement). Large file; splitting is tracked separately.

## Contents

- Core tools: `bash.py`, `filesystem.py`, `file_edit.py`, `web.py`, `memory.py`, `todo.py`, `notify.py`, `think.py`
- Optional tools: `browser.py`, `calendar.py`, `outlook_email.py`, `google_docs.py`, `google_sheets.py`, `http_api.py`, `claude_code.py`, `twitch.py`, `image_generation.py`
- Service integrations: `*_service_integrations.py` — grouped by domain (each file is large)
- Auth helpers: `auth_manager.py`, `auth_cache_utils.py`, `*_auth.py`
- `definitions/` — schema helpers for custom tools and MCP tool schemas
- `spawn_thread.py` — callable thread invocation
- `slash_command.py` — agent-side slash command tool

## Notes

New tools: decorate with `@tool` from `langchain_core.tools`, then add to the appropriate list in `__init__.py`.
