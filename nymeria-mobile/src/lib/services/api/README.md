# API Service Modules

`api.svelte.ts` is only the compatibility entrypoint for existing mobile imports.
The concrete `NymeriaAPI` facade lives in `index.ts` and extends the domain
classes in this directory.

## Modules

- `base.ts` - shared transport helpers, auth/error handling, response normalizers, and `probeConnection`.
- `system.ts` - health, auth verification, restart, settings, and model metadata.
- `accounts.ts` - profile, tokens, admin users, platform identities, Chat App bindings, and BYO Telegram bots.
- `chat.ts` - chat streaming, stream abort state, attachment validation, workspace download, and sync chat.
- `threads.ts` - thread listing, metadata, history, stop, and context stats.
- `todos.ts` - TODOs, activity, notification, and dashboard task-count endpoints.
- `tools.ts` - builtin, custom, unified, optional, and default tool endpoints.
- `mcp.ts` - MCP server CRUD, discovery, and testing.
- `thread-config.ts` - thread config and callable agent thread templates.
- `skills.ts` - installed skills, thread skills, callable tools, and global skills.
- `triggers.ts` - trigger CRUD, sources, tests, and execution history.
- `reporting.ts` - facade extension point kept for parity with desktop's module chain.

Existing callers should keep importing from `$lib/services/api.svelte` unless a
new internal module has a specific reason to depend on one domain class.
