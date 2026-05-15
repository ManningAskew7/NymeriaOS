---
name: self-improve
description: Expand Nymeria's capabilities by discovering tools, managing MCP servers and skills, probing APIs, and authoring durable Skill Kits.
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - tool_search
      - tool_enable
      - manage_mcp
      - skill_manage
      - api_discover
      - http_request
      - skill_kit_create
    tool_ttl: 2h
---

# Capability Expansion

Core question: does Nymeria already have the capability, or does it need to be
added?

First decide whether the user has a one-off need or a repeating workflow.
For a one-off, prefer enabling or installing the minimum capability with an
intentional TTL. For a repeating workflow, save the capability as a Skill or
Skill Kit so future threads do not rediscover the same tools and instructions.

The user should not need to know tool names, schemas, YAML, MCP configuration,
or Skill Kit mechanics.

## Path 1: Enable Existing Tools

Use this when the capability already exists in Nymeria's tool registry.

Flow: `tool_search` -> `tool_enable`.

1. Search by the user-visible task, not by guessed tool names.
2. Enable only exact tools you need, or a category only when the whole category
   is appropriate.
3. Pass a deliberate `ttl` every time you call `tool_enable(action="enable")`.
4. If a reload is queued, stop after that tool result and continue only after
   the automatic resume.

Also check installed skills with `skill_manage(action="search"|"list")` when
the user is asking for a repeatable workflow rather than a single tool.

## Path 2: Install MCP Servers

Use this when Nymeria needs an external service, local app, database, browser,
or API surface that is better exposed as an MCP server.

Flow: `manage_mcp(action="search")` or `web_search` -> `manage_mcp(action="preview")`
-> `manage_mcp(action="install")`.

Start with `manage_mcp(action="search")` for official registry and Smithery
results. If the registry search misses, use `web_search` to find official docs
or GitHub repositories for MCP servers; `manage_mcp(action="install")` can
often install directly from a repo URL, command, package page, HTTP endpoint,
or Claude Desktop JSON snippet.

Install only when an MCP server is the right fit. If the user only needs one
documented HTTP call, use `api_discover` and `http_request` instead.

## Path 3: Create A Skill or Skill Kit

Use this when you have solved a problem that will likely come up again.

Why it matters: a Skill saves the instructions. A Skill Kit saves the
instructions plus `required_tools`, so future threads get the right tool
schemas automatically when the skill is activated. That prevents repeated
search, enable, and setup work.

Flow: `draft` -> `validate` -> `publish`.

Use `skill_kit_create(action="draft")` to save a draft, `validate` to check it,
and `publish` when ready. Use `package` only for the shortcut case where the
draft and publish are both clearly safe.

Choose the artifact:

- Enable tools only: the need is one-off and the instructions are obvious.
- Plain Skill: the reusable value is instructions only; leave `required_tools`
  empty.
- Skill Kit: future runs need specific Nymeria tools; put exact tool names in
  `required_tools` and set `tool_ttl`.
- Custom HTTP tool plus Skill Kit: the reusable workflow needs a parameterized
  API call. Draft, test, and publish the HTTP tool first, then package a Skill
  Kit around it.

## TTL Reasoning

Match the TTL to how long this task or workflow should need the tool.

- Minutes to a short session: `30m` or `2h`.
- Work that will continue across the day: `6h` or `24h`.
- Multi-day project: `7d`, `14d`, or another day/week value.
- Stable personal workflow: `never`.

When unsure, match the TTL to the user's stated deadline, the TODO timeline,
or the expected project duration. Do not default blindly.

## Skill Kit Writing Rules

- Prefer user scope. Use global scope only when the user explicitly asks and
  the current user is an admin.
- Name the durable capability, not the current one-off task. Example:
  `weather-alerts`, not `today-weather`.
- Keep the `description` clear about when to use the skill. Nymeria sees this
  before loading the full skill body.
- Put exact Nymeria tool names in `required_tools`. Do not put portable
  `allowed-tools` there, and do not invent tool names.
- Set `tool_ttl` to `Nm`, `Nh`, `Nd`, `Nw`, or `never` (`permanent` is also accepted).
- Ask a plain-language confirmation before publishing anything that uses
  secrets, paid APIs, destructive actions, messages sent to other people, or
  global scope.
- Do not use `claude_code`, `reload_all`, or rollback tools as part of this
  workflow. Codebase self-modification is a separate admin-only path.

## Example: Wrap An Existing Tool

The tool already exists and the workflow is likely to recur, so package the
tool with short operating instructions:

```json
{
  "action": "package",
  "name": "trigger-monitoring",
  "description": "Inspect and troubleshoot recurring Nymeria trigger failures.",
  "required_tools": ["trigger_info"],
  "tool_ttl": "7d",
  "body": "# Trigger Monitoring\n\nUse trigger_info to inspect recent trigger executions before changing configuration. Summarize whether the failure is source polling, conditions, cooldown, or delivery.",
  "activate_current_thread": true
}
```

## Example: Create Then Bundle An HTTP Tool

The user needs a reusable API lookup. First prove the endpoint with
`api_discover` or `http_request`, then draft the reusable HTTP tool:

```json
{
  "action": "draft_http_tool",
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

Then test and publish the HTTP tool:

```json
{
  "action": "test_http_tool",
  "draft_id": "public_status_lookup",
  "sample_params": {"service": "example"}
}
```

```json
{
  "action": "publish_http_tool",
  "draft_id": "public_status_lookup"
}
```

Finally package a Skill Kit requiring the published tool:

```json
{
  "action": "package",
  "name": "service-status-checks",
  "description": "Check public service status pages and explain incidents.",
  "required_tools": ["public_status_lookup"],
  "tool_ttl": "24h",
  "body": "# Service Status Checks\n\nUse public_status_lookup with the service slug. Explain current status, active incidents, and stale or missing data clearly.",
  "activate_current_thread": true
}
```

## Output

Tell the user what capability was created in plain language, the new Skill or
Skill Kit name, and whether it is active on this thread. Mention any limitation
that still requires user input, such as credentials or an unavailable API.
