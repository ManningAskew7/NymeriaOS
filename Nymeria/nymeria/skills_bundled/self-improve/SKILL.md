---
name: self-improve
description: Build durable Nymeria capabilities from plain-language user requests by discovering APIs, creating tested tools, and packaging them into Skill Kits.
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - api_discover
      - http_request
      - tool_create
      - skill_config
    tool_ttl: 2h
---

# Self Improve

Use this skill when the user asks Nymeria to gain a durable new capability,
integration, reusable workflow, or future behavior. The user should not need
to know tool names, API schemas, YAML, or Skill Kit mechanics.

## Workflow

1. Decide whether an existing tool or skill already covers the request. Use
   existing capabilities when they are sufficient; do not create duplicates.
2. If a new HTTP/API tool is needed, discover the API first:
   - Use `api_discover` when there is an API base URL or docs URL.
   - Use `http_request` for one-off probes against documented endpoints.
   - Do not include secrets in headers or payloads unless the user explicitly
     provided them for this request.
3. After a working request shape is proven, create a reusable tool:
   - `tool_create(action="draft", ...)`
   - `tool_create(action="test", draft_id=..., sample_params=...)`
   - `tool_create(action="publish", draft_id=...)`
   Only publish after a successful test.
4. Package the durable workflow as a Skill Kit with `skill_config`.
   Include concise instructions, exact required tool names, and examples only
   where they reduce mistakes. Use `activate_current_thread=true` so the new
   Skill Kit is immediately available in this conversation.
5. Verify the result. If the new Skill Kit activates with a reload, stop after
   the publish result and continue only after the automatic resume.

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

## Example: Wrap An Existing Tool

If the needed tool already exists:

```json
{
  "action": "publish",
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

Then test, publish, and create a Skill Kit requiring the published tool:

```json
{
  "action": "publish",
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
