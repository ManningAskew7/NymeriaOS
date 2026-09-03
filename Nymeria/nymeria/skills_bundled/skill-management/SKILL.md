---
name: skill-management
description: Find, install, create, and edit Skills and Skill Kits. Load this
  when a capability or workflow will come up again and should be saved as
  durable instructions, optionally bundled with the Nymeria tools it needs, so
  future threads do not rediscover the setup, whether to search and install
  existing skills, author a new Skill or Kit, or revise one. Not for a one-off
  task, and not for unattended code routines (use workflow-authoring).
metadata:
  nymeria:
    required_tools:
      - skill_manage
      - skill_write
      - skill_edit
    tool_ttl: 2h
---

# Skill Management

Use this kit to save a repeating capability as a Skill or a Skill Kit, or to find
and install one that already exists.

Why it matters: a plain Skill saves the instructions. A Skill Kit saves the
instructions plus `required_tools`, so when the skill is activated, future threads
get the right Nymeria tool schemas automatically. That removes repeated search,
enable, and setup work.

## Find First

Before authoring, check what is already installed:

- `skill_manage(action="search")` or `skill_manage(action="list")` to find an
  existing Skill or Kit for the user's workflow.
- `skill_manage(action="install")` to install one that fits.

Author a new skill only when nothing suitable exists.

## Choose the Artifact

- Enable tools only (no skill): the need is one-off and the instructions are
  obvious. Use `tool-management` instead and stop.
- Plain Skill: the reusable value is instructions only. Leave `required_tools`
  empty.
- Skill Kit: future runs need specific Nymeria tools. Put the exact tool names in
  `required_tools` and set `tool_ttl`.
- Custom tool plus Skill Kit: the workflow needs a parameterized API call or a
  small Python helper. Build and publish the tool first via `tool-management`, then
  package a Skill Kit around the published tool here.

## Write or Edit

Flow: write the full SKILL.md markdown -> `skill_write`.

- `skill_write` creates a new Skill or Skill Kit from SKILL.md markdown.
- `skill_edit` revises an existing Skill: its body, description, name, or required
  tool metadata. Use it when the skill already exists and only the content changes.

### Skill Kit writing rules

- Prefer user scope. Use global scope only when the user explicitly asks and is an
  admin.
- Name the durable capability, not the current one-off task. Example:
  `weather-alerts`, not `today-weather`.
- Keep the `description` clear about when to use the skill. Nymeria reads it before
  loading the full body, so it is what semantic search matches on.
- Put exact Nymeria tool names in `metadata.nymeria.required_tools`. Do not invent
  tool names, and do not put portable `allowed-tools` there.
- Set `tool_ttl` to `Nm`, `Nh`, `Nd`, `Nw`, `never`, or `permanent`.
- Write the body as operating instructions a future thread can follow without
  context from this conversation.

### Example: wrap an existing tool in a Kit

The tool already exists and the workflow is likely to recur, so bundle the tool with
short operating instructions:

```json
{
  "markdown": "---\nname: trigger-monitoring\ndescription: Inspect and troubleshoot recurring Nymeria trigger failures.\n---\n\n# Trigger Monitoring\n\nUse trigger_info to inspect recent trigger executions before changing configuration. Summarize whether the failure is source polling, conditions, cooldown, or delivery.",
  "tools": ["trigger_info"],
  "tool_ttl": "7d",
  "activate_current_thread": true
}
```

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

- You need to find, enable, or build the tools a Kit should require ->
  `tool-management`.
- The capability is an external service better exposed as a server ->
  `mcp-management`.
- A tool or server needs a credential -> `credential-management`.
- The user wants a persistent, isolated, re-invocable specialist ("my email
  manager") rather than inline instructions -> `callable-thread-builder`.
- For the overall operating philosophy and when to create which artifact ->
  `Skill(name="self-improve", ttl="1h")`.

## Output

Tell the user in plain language what was saved, the Skill or Skill Kit name, and
whether it is active on this thread. Mention any limitation that still needs them,
such as a credential or an unavailable API.
