---
name: trigger-management
description: Configure, inspect, test, and troubleshoot Nymeria triggers,
  automations that fire on a schedule, an RSS/Atom feed, a webhook, email, or
  chat activity. Load this when the user wants Nymeria to watch or poll
  something and act when it changes, or asks why a trigger is silent or
  failing. Not for a one-off reminder (nym_todo) or for in-turn hooks
  (hook-management).
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - trigger_config
      - trigger_info
    tool_ttl: 2h
---

# Trigger Management

Use this skill when the user wants to create, inspect, modify, test, or debug
Nymeria triggers.

## Workflow

1. Inspect first. Use `trigger_info(action="list")` for existing triggers and
   `trigger_info(action="sources")` when you need available source plugins or
   schema details.
2. Configure with `trigger_config`. Prefer updates to existing triggers when
   the user's intent is to adjust behavior rather than create a separate
   automation.
3. Test before treating the work as complete. Use the trigger test path when a
   trigger has sampleable source data, then explain any source limitations.
4. Keep thread scope explicit. When a trigger should post into a specific
   conversation, confirm or infer the thread id from the current task context.
5. For troubleshooting, inspect recent executions and the trigger definition
   before changing config. Report whether the issue is source polling,
   conditions, cooldown, delivery, or target-thread behavior.

## Output

Summarize the trigger name, enabled state, source, condition, action, cooldown,
and where results will be delivered. Mention test results or the reason a test
could not be run.
