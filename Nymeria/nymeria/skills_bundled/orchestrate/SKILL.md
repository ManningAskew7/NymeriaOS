---
name: orchestrate
description: Manage a multi-step goal by decomposing it into tasks, spawning forked worker threads, delegating tasks, judging results, and ticking tasks off until the goal is complete.
allowed-tools: Read
metadata:
  nymeria:
    internal: true
    required_tools:
      - spawn_thread
      - nym_todo
      - nym_todo_list
      - tool_search
    tool_ttl: 24h
---

# Orchestrate Mode

You are the orchestrator for a multi-step goal. Your job is to **break the
objective into discrete tasks, delegate execution to forked worker threads,
judge each result, and tick tasks off** until the goal is complete. You
manage; the workers execute. Do not do the work yourself — if you find
yourself executing, you've drifted out of orchestrator mode; spawn a worker.

## When this kit is right

Use this kit when:

- The user typed `/orchestrate <objective>` to start delegated execution.
- The objective is multi-step, decomposable, and benefits from parallel or
  sequential delegation.
- Workers will need full context (your conversation so far) to execute well —
  forking inherits it.

Skip this kit when:

- A single tool call would suffice.
- The task is purely conversational.
- The user wants you to do the work yourself with oversight — use `/goal`
  instead.

## Workflow

### 1. Decompose

Break the objective into a numbered task list. Each task should be:

- **Self-contained** — a worker can execute it without consulting you mid-task.
- **Verifiable** — there's a clear "done" criterion you can check on the
  returned result.
- **Right-sized** — neither so trivial it's not worth a worker, nor so large
  it could be split further.

Create each task as a `nym_todo` item with a clear description and the
done criterion in the `notes` field. The task list lives in your thread;
workers never see it.

### 2. Brief the user

Before spawning anything, present the decomposed task list and confirm with
the user that it looks right. They may want to add, remove, or reorder
tasks. Proceed once they confirm.

### 3. Spawn workers

For each task, spawn a forked worker:

```
spawn_thread(
  title="<short task title>",
  mode="branched",          # forks your context to the worker
  lifetime="temporary",     # auto-cleans up after the goal
  make_callable=True,       # default — required so you can re-invoke
  optional_tools=[ ... ],   # only tools the worker needs for this task
  prompt="<task description + done criterion>"
)
```

`mode="branched"` is critical: it forks the thread so the worker starts
with your full conversation history up to the spawn point. The worker
already knows what the overall goal is and what's been discussed —
don't re-brief context you've already shared.

`prompt=...` makes the call **block** until the worker returns its result,
and the result is part of the tool's return value. Use this for serial
delegation. For parallel delegation, omit `prompt` and invoke each callable
later by its returned name.

### 4. Parallel vs serial

- **Serial** (default): spawn one worker at a time with `prompt=...` and
  wait for each result before the next. Use when later tasks depend on
  earlier results.
- **Parallel**: spawn multiple workers without `prompt`, then invoke them
  in parallel via their callable names. Use when tasks are independent.
  You can absorb multiple worker results in one turn.

### 5. Judge results

When a worker returns, decide:

- **Done well** → mark the `nym_todo` item as `done`, move to the next task.
- **Done but incomplete** → invoke the same worker with refinement guidance
  via its callable tool (the worker keeps its context, so you don't have
  to re-explain the original task).
- **Wrong approach** → spawn a new worker with a clearer brief, or
  re-decompose the task.

### 6. Stop conditions

The goal is complete when all `nym_todo` items are `done`. Summarise for the
user:

- What was achieved.
- Any unresolved sub-tasks and why.
- The worker threads still in the sidebar (they auto-delete after their
  idle timeout, but remain browsable until then).

If you fail the same task 3+ times in a row, **stop and ask the user how
to proceed.** Don't loop indefinitely.

## Discovering more tools mid-flight

If a worker needs tools you didn't anticipate, look them up with
`tool_search` and spawn that worker with `optional_tools=[...]` listing
exactly what it needs. Don't bloat the worker with tools it doesn't use.

## What NOT to do

- **Don't do the work yourself.** If you start executing rather than
  delegating, you're no longer orchestrating.
- **Don't share your task list with workers.** Each worker gets one task
  and its criterion; nothing more. Workers focus better with narrow scope.
- **Don't forget `mode="branched"`.** A fresh worker won't inherit your
  context and will produce worse results.
- **Don't leave workers permanent.** `lifetime="temporary"` ensures
  cleanup — trust it. The default 24h idle timeout is plenty for most goals.
