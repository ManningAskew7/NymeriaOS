---
name: tool-management
description: Find, enable, and build Nymeria tools. Use this skill to give Nymeria a
  new capability that is a tool, whether by enabling an existing tool or category from
  the registry, or by probing an HTTP API and wrapping it as a reusable custom tool.
  Load it whenever the user needs something Nymeria cannot currently do and the answer
  is "a tool" (not an MCP server, not a reusable skill, not a credential).
metadata:
  nymeria:
    required_tools:
      - tool_search
      - tool_manage
      - tool_create
      - api_discover
      - http_request
    tool_ttl: 2h
---

# Tool Management

Use this kit to find, enable, or build a Nymeria tool. The user should never need
to know tool names, schemas, categories, or JSON; translate their plain-language
need into the right tool.

First decide: does the capability already exist in Nymeria's registry, or does it
need to be built?

- Already exists -> Path 1 (enable it).
- A documented HTTP/API endpoint the user will reuse -> Path 2 (probe, then wrap).
- A small deterministic transform -> Path 2 (a Python helper tool).

## Path 1: Find and Use Existing Tools

Flow: `tool_search` -> then either RUN it once (`tool_invoke`) or BIND it
(`tool_manage(action="enable", ttl=...)`).

1. Search by the user-visible task, not by guessed tool names. `tool_search`
   matches on intent; describe what the user wants to do. Add
   `include_schemas=true` once you know which result you want, to get its
   argument schema in the result.
2. Decide how to execute it (see "Defer vs bind" below): run it once through
   `tool_invoke` for a one-off, or enable it for repeated use.
3. When binding, enable only the exact tools you need, and pass a deliberate
   `ttl` on every `tool_manage(action="enable")` call (see TTL reasoning below).
   Never default it blindly. Enable a whole category only when the entire
   category genuinely fits the task.
4. If enabling queues a tool reload, stop after that tool result and continue only
   after the automatic resume.

If the tool exists but fails because it needs authentication (an API key, OAuth
login, or other secret), switch to `credential-management` to request and bind the
credential, then retry.

### Defer vs bind

Two ways to execute a tool that is not already on the thread:

- DEFER (cache-safe): `tool_invoke(name, arguments)` runs the tool once without
  adding it to your tool list. Nothing is bound, so the prompt cache is
  preserved. Best for a one-off call, a short horizon, or while you are still
  choosing among several candidate tools. Get the argument schema from
  `tool_search(include_schemas=true)`; `tool_invoke` validates your arguments
  and, if they are wrong, echoes the correct schema back so you can retry. Note:
  deferred arguments are validated but NOT grammar-constrained as you type them,
  so format them carefully from the schema.
- BIND (first-class): `tool_manage(action="enable", ttl=...)` adds the tool to
  the thread so the model can call it directly with grammar-constrained
  arguments. Best for repeated use over a longer conversation, or when the
  arguments are complex or fragile. Binding changes the cached tool prefix
  once; a bound schema sits in that prefix and is read as your tool grammar,
  while a deferred schema is prose in history, which is why binding calls
  more reliably.

When unsure: one call now -> defer; several calls over a real task -> bind.

## Path 2: Build a New Tool

Use this when no existing tool fits and the capability is a reusable API call or a
small pure helper. The dominant pattern is probe-then-wrap: prove the endpoint
works before you wrap it.

### Wrap an HTTP API

Flow: `api_discover` or `http_request` (prove it) -> `tool_create` draft -> test ->
publish.

1. Probe first. Use `api_discover` to understand an API surface, or `http_request`
   to confirm a single endpoint returns what you expect.
2. Draft the tool with `tool_create`, parameterizing the URL with `${...}` fields:

```json
{
  "action": "draft",
  "tool_id": "public_status_lookup",
  "name": "Public Status Lookup",
  "description": "Look up a public service status by service slug.",
  "parameters": {
    "service": {
      "type": "string",
      "description": "Service slug from the public status API",
      "required": true
    }
  },
  "http_config": {
    "method": "GET",
    "url": "https://status.example.com/api/${service}",
    "response_format": "json"
  }
}
```

3. Test the draft, then publish only if the test passes:

```json
{"action": "test", "draft_id": "public_status_lookup", "sample_params": {"service": "example"}}
```
```json
{"action": "publish", "draft_id": "public_status_lookup"}
```

### Create a Python helper

Use Python custom tools only for small pure helpers. Keep all work inside the
entrypoint function; no top-level calls, background processes, or raw secrets.

```json
{
  "action": "draft",
  "implementation_type": "python",
  "tool_id": "slugify_text",
  "name": "Slugify Text",
  "description": "Convert text into a lowercase URL slug.",
  "parameters": {
    "text": {"type": "string", "description": "Text to convert", "required": true}
  },
  "python_code": "import re\n\ndef run(text: str) -> str:\n    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n    return slug"
}
```

Then `tool_create(action="test", draft_id="slugify_text", sample_params={"text": "Hello Nymeria"})`,
and publish only if the test passes.

A published custom tool is just a tool. If the workflow around it will recur, hand
off to `skill-management` to package a Skill Kit that requires the new tool, so
future threads get it with operating instructions automatically.

## TTL Reasoning

Match the TTL to how long this task or workflow should keep the tool.

- Minutes to a short session: `30m` or `2h`.
- Across the day: `6h` or `24h`.
- Multi-day project: `7d`, `14d`, or another day/week value.
- Stable personal workflow: `never`.

When unsure, match the TTL to the user's stated deadline, the TODO timeline, or the
expected project duration.

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

- The capability is an external service, local app, database, or browser better
  exposed as a server -> `mcp-management`.
- You have solved a repeating workflow and want to save the tools plus
  instructions -> `skill-management`.
- The repeating routine should run as saved CODE, unattended and multi-step
  (poll something, chain tools, approval checkpoints) -> `workflow-authoring`.
- A tool needs an API key, OAuth login, or other credential -> `credential-management`.
- The user wants a persistent, re-invocable specialist (not just a tool) ->
  `callable-thread-builder`.
- For the overall operating philosophy and when to create which artifact ->
  `Skill(name="self-improve")`.

## Output

Tell the user in plain language what capability now exists, the tool name if you
created one, and how long it is available (the TTL). Mention any limitation that
still needs them, such as a credential or an unavailable endpoint.
