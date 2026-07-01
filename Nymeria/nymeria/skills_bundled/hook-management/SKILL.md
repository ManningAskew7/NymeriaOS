---
name: hook-management
description: Create, inspect, test, and troubleshoot Nymeria lifecycle hooks.
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - hook_config
      - hook_info
    tool_ttl: 2h
---

# Hook Management

Use this skill when the user wants to create, inspect, modify, test, or debug
lifecycle hooks: small rules that inject a string into your context when an
event fires.

## What a hook is

A hook is three parts: WHEN an event fires, run a LOGIC action, which RETURNS an
injected string. This pass ships exactly one action, `inject_context`, over
three events:

- `prompt_submit` -- text is appended to the model-facing message tail at the
  start of every turn (good for standing reminders or context).
- `post_tool_use` -- text is appended to a tool's result. Use `matcher` (a
  pipe-list of tool names, e.g. `Edit|Write`) to scope it to specific tools;
  omit `matcher` to annotate every tool.
- `done` -- when a turn finishes, the text is re-driven as a single follow-up
  prompt (a "run the checks now that you're done" style nudge). It batches with
  any user prompts queued in the meantime.

PreToolUse is not an injection target and is not offered.

The text is static or templated: `{placeholder}` tokens interpolate from the
event context (`{tool_name}`, `{tool_result}`, `{tool_status}`, `{tool_args}`,
`{prompt}`, `{final_text}`, `{thread_id}`, `{user_id}`, `{event}`). Unknown
placeholders are left verbatim, and plain text with no braces passes through
untouched.

## Workflow

1. Inspect first. `hook_info(action="list")` shows existing hooks;
   `hook_info(action="detail", hook_id=...)` shows one hook's full config.
2. Configure with `hook_config`. Create needs `name`, `event`, and `text`;
   `matcher` applies to `post_tool_use` only; `scope` is `thread` (default,
   this conversation) or `global` (all of the user's threads). Prefer updating
   an existing hook when the intent is to adjust behavior.
3. Test before treating the work as complete. `hook_info(action="test",
   hook_id=...)` renders the text against sample data (no fire) so you can
   confirm the template resolves as intended.
4. Keep scope explicit. Thread-scoped hooks bind to the current thread
   (including the `default` thread). Confirm whether the user wants this
   conversation only or all of them.
5. For troubleshooting, check the hook's `enabled` state and scope first, then
   whether a per-thread override or the master kill switch has turned it off.
   A `matcher` set on a non-tool event never matches and is dropped on save.

## Output

Summarize the hook name, id, event, matcher (if any), scope, enabled state, and
where the rendered text lands. Mention the test render result, or the reason a
test could not be run.
