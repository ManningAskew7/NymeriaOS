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

# Capability Expansion / Skill Kit Authoring

Use this skill when the user asks Nymeria to gain a capability it does not
already have: finding and enabling tools, installing or enabling skills,
finding or installing MCP servers, probing an API, or creating a reusable
Skill Kit. The user should not need to know tool names, schemas, YAML, MCP
configuration, or Skill Kit mechanics.

## Workflow

1. Check whether an installed capability already covers the request:
   - Use `tool_search` to find local, optional, custom, or discovered MCP tools.
   - Use `tool_enable` only after you know the exact tool names or category.
   - Use `skill_manage(action="search"|"list")` for installed or marketplace skills.
   - Use `manage_mcp(action="search"|"inspect")` for MCP server capabilities.
2. Enable existing capabilities before creating new ones:
   - `tool_enable(action="enable", tools=[...])` for tools.
   - `skill_manage(action="enable", name=...)` for installed skills.
   - `manage_mcp(action="install", ...)` only when an MCP server is the right fit.
3. If a new HTTP/API tool is needed, discover and test the API first:
   - Use `api_discover` when there is an API base URL or docs URL.
   - Use `http_request` for one-off probes against documented endpoints.
   - Do not include secrets in headers or payloads unless the user explicitly
     provided them for this request.
4. Create durable capabilities through `skill_kit_create`:
   - Use `skill_kit_create(action="draft_http_tool")`, `test_http_tool`, and
     `publish_http_tool` for reusable HTTP tools.
   - Use `skill_kit_create(action="package"|"publish", required_tools=[...])`
     to package existing or newly published tools into a Skill Kit.
   - Include concise instructions, exact required tool names, and examples only
     where they reduce mistakes.
   - Use `activate_current_thread=true` so the new Skill Kit is immediately
     available in this conversation.
5. Verify the result. If enabling, installing, or publishing queues a reload,
   stop after that tool result and continue only after the automatic resume.

## Skill Kit Writing Rules

- Prefer user-scope Skill Kits. Use global scope only when the user explicitly
  asks and the current user is an admin.
- A generated Skill Kit should name the durable capability, not the current
  one-off task. Example: `weather-alerts`, not `today-weather`.
- Keep the `description` clear about when to use the skill. This is the text
  Nymeria sees before loading the full skill body.
- Put exact Nymeria tool names in `required_tools`. Do not put portable
  `allowed-tools` there, and do not invent tool names.
- Ask a plain-language confirmation before publishing anything that uses
  secrets, paid APIs, destructive actions, messages sent to other people, or
  global scope.
- Do not use `claude_code`, `reload_all`, or rollback tools as part of this
  workflow. Codebase self-modification is a separate admin-only path.

## Example: Wrap An Existing Tool

If the needed tool already exists:

```json
{
  "action": "package",
  "name": "trigger-monitoring",
  "description": "Inspect and troubleshoot recurring Nymeria trigger failures.",
  "required_tools": ["trigger_info"],
  "tool_ttl": "2h",
  "body": "# Trigger Monitoring\n\nUse trigger_info to inspect recent trigger executions before changing configuration. Summarize whether the failure is source polling, conditions, cooldown, or delivery.",
  "activate_current_thread": true
}
```

## Example: Create Then Bundle An HTTP Tool

After `http_request` proves the endpoint works:

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
  "body": "# Service Status Checks\n\nUse public_status_lookup with the service slug. Explain current status, active incidents, and stale or missing data clearly.",
  "activate_current_thread": true
}
```

## Output

Tell the user what capability was created in plain language, the new Skill Kit
name, and whether it is active on this thread. Mention any limitation that
still requires user input, such as credentials or an unavailable API.
