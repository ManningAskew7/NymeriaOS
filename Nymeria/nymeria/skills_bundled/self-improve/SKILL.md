---
name: self-improve
description: The operating philosophy and safety rules for expanding Nymeria's own
  capabilities, and a router to the focused capability kits. Load this to decide
  whether Nymeria already has a capability or needs a new one, which artifact to
  create, and which management kit to use. This skill carries guidance only; it binds
  no tools.
allowed-tools: Read
---

# Capability Expansion

This is the guidance layer for giving Nymeria new capabilities. It binds no tools.
It tells you how to think about capability expansion and routes you to the focused
kit that holds the right tools. The user should never need to know tool names,
schemas, YAML, MCP configuration, or Skill Kit mechanics; translate their plain
request into the right path.

## Core question

Does Nymeria already have the capability, or does it need to be added?

Then decide one-off versus repeating:

- One-off need: enable or install the minimum capability with a deliberate TTL.
- Repeating workflow: save it as a Skill or Skill Kit so future threads do not
  rediscover the same tools and instructions.

## Which kit when

Load the kit that matches the task and follow its instructions:

| The user needs... | Load this kit |
|-------------------|---------------|
| To find, enable, or build a tool, including wrapping an HTTP/API endpoint | `tool-management` |
| An external service, local app, database, browser, or API surface as a server | `mcp-management` |
| To find, install, create, or edit a Skill or Skill Kit | `skill-management` |
| An API key, OAuth login, or other credential (or a tool failed for missing auth) | `credential-management` |
| A persistent, isolated, re-invocable specialist ("my email manager") | `callable-thread-builder` |

If the first kit turns out to be the wrong one, each kit ends with a "Not the right
kit?" pointer to its siblings, so you can re-route without coming back here.

## Save the capability

After you assemble a capability that will recur, do not let it evaporate at the end
of the thread. Choose how to keep it:

- Skill Kit (via `skill-management`) when the value is tools plus how to use them
  and you work inline.
- Callable thread (via `callable-thread-builder`) when the user wants a persistent,
  isolated specialist they can re-invoke by name.
- One-off: neither. Just use the tools with an intentional TTL and move on.

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

Each capability kit repeats these rules in its own body, so they hold even when a
kit is activated directly without loading this skill first.

## Output

Tell the user in plain language what capability now exists, the Skill, Skill Kit, or
tool name if you created one, and whether it is active on this thread. Mention any
limitation that still needs them, such as a credential or an unavailable API.
