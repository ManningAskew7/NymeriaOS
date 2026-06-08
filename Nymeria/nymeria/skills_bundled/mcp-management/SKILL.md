---
name: mcp-management
description: Find, install, test, and manage MCP servers. Use this skill when Nymeria
  needs an external service, local app, database, browser, or API surface that is best
  exposed as an MCP server, rather than a single tool call. Load it to search public
  MCP registries, preview and install a server, inspect or test an installed server,
  or enable, disable, and remove servers.
metadata:
  nymeria:
    required_tools:
      - manage_mcp
    tool_ttl: 2h
---

# MCP Management

Use this kit to connect Nymeria to an external service through an MCP server.
`manage_mcp` is a single action-dispatch tool; pass the right `action`.

First decide whether an MCP server is actually the right fit. Install one when the
user needs a whole service surface (a database, a browser, a local app, a rich API).
If the user only needs one documented HTTP call, do not install a server; route to
`tool-management` and wrap the endpoint as a custom tool instead.

## Flow

Discovery (read-only): `manage_mcp(action="search", query="...")` searches public
registries; `manage_mcp(action="preview", source="...")` shows what a source would
install. If registry search misses, the user can point you at official docs or a
GitHub repo, and install can often accept a repo URL, command, package page, HTTP
endpoint, or a Claude Desktop JSON snippet as `source`.

Install: `manage_mcp(action="install", source="...", config_values={...})`.
- Higher-risk installs require `confirmed=true`. Preview and explain the plan to the
  user, get a plain-language go-ahead, then install with `confirmed=true`.
- If the server needs API keys or other secrets, hand off to
  `credential-management` to request and bind them (or pass them through
  `configure_credentials`), then retry.
- Install can queue a tool reload. When the result says a reload is queued, stop
  after that tool result and continue only after the automatic resume.

Diagnose: `manage_mcp(action="inspect")` lists installed servers and their state;
`logs` and `test` help troubleshoot a connection; `retry` re-attempts a failed
discovery.

Lifecycle (admin-gated): `enable`, `disable`, and `delete` change server state.
These require an admin caller. Confirm with the user before disabling or deleting.

## Safety

Before acting, confirm in plain language with the user when an action:
- uses or stores secrets, credentials, or paid/billable APIs;
- is destructive or hard to reverse (deletes, disables, overwrites);
- sends messages to other people or posts to external services;
- changes anything at global scope (prefer user scope; use global only when the
  user explicitly asks and is an admin).

Prefer the minimum capability with a deliberate TTL over a broad or permanent
grant. Do not use `claude_code`, `reload_all`, or rollback tools as part of this
workflow; changing Nymeria's own codebase is a separate, admin-only path.

## Not the right kit?

- The user only needs one documented HTTP call -> `tool-management` (probe with
  `http_request`/`api_discover`, then wrap with `tool_create`).
- The server needs an API key, OAuth login, or other secret ->
  `credential-management`.
- You want to save a recurring MCP-backed workflow as reusable instructions ->
  `skill-management`.
- For the overall operating philosophy and when to create which artifact ->
  `Skill(name="self-improve")`.

## Output

Tell the user in plain language what server is now connected, what it can do, and
whether its tools are active on this thread. Mention any limitation that still needs
them, such as a credential or an unreachable endpoint.
