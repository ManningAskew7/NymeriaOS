---
name: callable-thread-builder
description: Design, create, invoke, and maintain callable Nymeria helper threads.
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - spawn_thread
    tool_ttl: 2h
---

# Callable Thread Builder

Use this skill when the user wants a reusable Nymeria helper thread, specialist
agent, delegated workflow, or callable tool backed by a thread.

## Workflow

1. Define the helper's job in operational terms: what it owns, what inputs it
   expects, what output shape it should return, and which tools it may use.
2. Use `spawn_thread(action="create")` when a new helper thread is needed.
   Set focused instructions and a clear title; leave `make_callable=true`
   unless the user only wants a background thread.
3. Keep tool access narrow. Pass only the tools needed for the helper's role.
   Use exact Nymeria tool names already known from the current context or
   discover them before spawning.
4. If the helper should run immediately, include an `initial_message` or invoke
   the returned callable tool after the spawn completes.
5. When updating or replacing an existing helper, inspect the current thread
   config and preserve user-authored instructions unless the user asked for a
   rewrite.

## Output

Report the spawned thread id, callable tool name, purpose, enabled tools, and
how the user should refer to it in future tasks. If creation fails, preserve the
error details and do not claim the callable exists.
