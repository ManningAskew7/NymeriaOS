# API Service Modules

`api.svelte.ts` is now only the compatibility entrypoint for existing imports.
The concrete `NymeriaAPI` facade lives in `index.ts` and extends the domain
classes in this directory.

## Modules

- `base.ts` - shared transport helpers, auth/error handling, response normalizers,
  and `probeConnection`.
- `system.ts` - health, auth verification, restart, settings, and model metadata.
- `accounts.ts` - profile, tokens, admin users, platform identities, Chat App
  bindings, and BYO Telegram bots.
- `chat.ts` - chat streaming, stream abort state, attachment validation, workspace
  download, and sync chat.
- `threads.ts` - thread listing, metadata, ownership claim, history, stop, and
  context stats.
- `todos.ts` - TODOs, activity, notification, and dashboard task-count endpoints.
- `tools.ts` - builtin, custom, unified, optional, and default tool endpoints.
- `mcp.ts` - MCP server CRUD, install, discovery, and testing.
- `thread-config.ts` - thread config, callable agent thread templates, teams, and
  import/export.
- `skills.ts` - installed skills, marketplace search/install, thread skills, and
  global skills.
- `triggers.ts` - trigger CRUD, sources, tests, and execution history.
- `reporting.ts` - problem reports.

Existing callers should keep importing from `$lib/services/api.svelte` unless a
new internal module has a specific reason to depend on one domain class.
