# Manual Testing with Chrome MCP

Guide for testing frontend changes via the `claude-in-chrome` MCP tools.

## Setup

1. Start the backend: `cd Nymeria && python run.py api` (run in background)
2. Wait for health check: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health`
3. Call `tabs_context_mcp` first to discover existing browser tabs
4. The desktop app runs at `http://localhost:1420/` (requires `npm run tauri dev` or a running Tauri window)

## Interacting with the UI

- Use `find` to locate elements by description (e.g., `"Save Changes button"`, `"Make Callable checkbox"`) — more reliable than coordinate clicks
- Use `ref`-based clicks (`left_click ref=ref_XX`) instead of coordinates when possible — modals and dynamic content shift positions
- Clicking on modal backdrops dismisses them — be precise when clicking checkboxes or buttons near edges
- After actions that change state (save, toggle), take a screenshot to verify the result
- Use `zoom` on small UI elements (badges, icons) to verify text content

## Verifying backend state via API

```bash
API_KEY=$(grep NYMERIA_API_KEY Nymeria/.env | cut -d= -f2)

# List callable threads and their names
curl -s -H "Authorization: Bearer $API_KEY" http://localhost:8000/agents/threads | python -m json.tool

# Check thread metadata (title, platform, title_source)
curl -s -H "Authorization: Bearer $API_KEY" "http://localhost:8000/threads?user_id=default" | python -m json.tool

# Check a specific thread's config
curl -s -H "Authorization: Bearer $API_KEY" "http://localhost:8000/threads/<id>/config" | python -m json.tool
```

## Common gotchas

- **Empty LLM responses**: Some models occasionally return 0-char responses — check logs for `[LLM] Response: 0 chars, final answer`. Verify tool binding with `grep 'Synced agent tools' data/logs/service.log`.
- **Callable thread tool names**: After renaming a callable thread, `sync_agent_tools()` must run for the LLM to see the new name.
- **Svelte 5 runes**: State uses `$state`, derived values use `$derived`. The `threadsStore` uses a closure pattern — module-scope functions can't access reactive state inside `createThreadsStore()`.
