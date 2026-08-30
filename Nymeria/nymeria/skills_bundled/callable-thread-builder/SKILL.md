---
name: callable-thread-builder
description: Design, create, invoke, and maintain callable Nymeria helper
  threads, and organize them into callable teams (create/rename teams, move
  threads between them) that scope which callables see each other. Load this
  to build a specialist helper thread, wire up a delegated workflow, or
  restructure the team bubbles your callables live in.
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - spawn_thread
      - team_manage
    tool_ttl: 2h
---

# Callable Thread & Team Builder

Use this skill when the user wants a reusable Nymeria helper thread, specialist
agent, delegated workflow, or callable tool backed by a thread, or wants their
callable threads organized into teams.

## Workflow

1. Define the helper's job in operational terms: what it owns, what inputs it
   expects, what output shape it should return, and which tools it may use.
2. Use `spawn_thread(action="create")` when a new helper thread is needed.
   Set focused instructions and a clear title; leave `make_callable=true`
   unless the user only wants a background thread.
3. Keep tool access narrow. Pass only the tools needed for the helper's role.
   Use exact Nymeria tool names already known from the current context or
   discover them before spawning.
4. If the helper should run immediately, include a `prompt` or invoke
   the returned callable tool after the spawn completes.
5. When updating or replacing an existing helper, inspect the current thread
   config and preserve user-authored instructions unless the user asked for a
   rewrite.

## Teams

Callable teams are isolation bubbles in BOTH directions: a teamed thread sees
only same-team callables, and an unteamed thread sees only unteamed callables
("no team" is itself a bubble). Use that to keep specialist crews from
polluting each other's tool lists.

- `team_manage` is the management surface: `list`, `show`, `create`,
  `rename`, `describe`, `add_thread`, `remove_thread`, `delete`. Teams are
  referenced by id or name; threads by id or callable name. Own-user scope.
- A thread spawned by `spawn_thread` inherits the SPAWNING thread's team.
  Override with `team=` on the spawn (`team="none"` spawns an unteamed child
  from a teamed parent).
- Before restructuring, `team_manage(action="list")` then `show` the affected
  team: moving a thread changes what every member can call, so say what
  visibility changes before doing a bulk move the user did not explicitly
  spell out.

## Output

Report the spawned thread id, callable tool name, purpose, enabled tools, team
(if any), and how the user should refer to it in future tasks. For team
changes, report the resulting membership. If creation fails, preserve the
error details and do not claim the callable exists.
