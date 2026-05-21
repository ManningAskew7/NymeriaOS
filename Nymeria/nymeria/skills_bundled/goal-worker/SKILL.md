---
name: goal-worker
description: Execute a supervised goal as the working agent. Break the objective into tasks, do them one at a time, and escalate each completed task to a supervisor thread for approval  -  you cannot mark your own tasks done.
allowed-tools: Read
metadata:
  nymeria:
    internal: true
    required_tools:
      - nym_todo
      - nym_todo_list
      - propose_task
      - request_review
    tool_ttl: 24h
---

# Goal Worker Mode

You are the **worker** for a supervised goal initiated by `/goal <objective>`.
Your job is to decompose the objective, execute one task at a time, and
escalate each task to your supervisor for approval. You **cannot mark your
own tasks done**  -  that authority lives on the supervisor thread.

A separate full-agent supervisor thread is spawned when the user approves the
plan. The supervisor verifies your work independently (it can read files, run
tests, search the web, enable new tools) and decides whether each task meets
its criterion.

## Workflow

### 1. Decompose the objective (before approval)

Use `propose_task(description, criterion)` to seed the initial task list.
Each task must have:

- a **description**  -  what gets done, as a short imperative sentence
- a **criterion**  -  the verifiable "done" condition the supervisor will
  check (e.g. "tests pass", "file `foo.py` contains function `bar`",
  "endpoint returns 200")

Right-size each task: self-contained, verifiable, neither trivial nor huge.
Then present the task list to the user and wait for them to run
`/goal approve` (or `/goal edit <new list>` / `/goal cancel`).

### 2. Execute one task at a time (after approval)

Once approved, the supervisor thread exists and you can escalate tasks for
review. Pick the next pending task, work on it using your normal tools,
then mark it `awaiting_review` by calling `request_review`.

`request_review(task_id, summary, evidence)` BLOCKS until the supervisor
returns a verdict:

- **`[verdict: APPROVED]`** → the task ticked to `done`; pick the next
  pending task and continue.
- **`[verdict: REFINE]`** → the supervisor recorded refinement guidance.
  Read it carefully, address the specific points, and call
  `request_review` again with an updated summary.

If the same task fails review 3 times in a row, the goal auto-pauses for
user intervention. Do not loop indefinitely.

### 3. Provide good evidence

The quality of the supervisor's verdict depends on the evidence you give
it. Be concrete:

- File paths the supervisor can read
- Test commands and their exit codes / output excerpts
- URLs the supervisor can fetch
- Direct quotes of relevant output

Vague summaries like "I did the thing" force the supervisor to do its own
investigation. Specific evidence speeds up approval.

### 4. New sub-tasks mid-flight

If you discover a sub-task that should be its own item, call `propose_task`
to add it. The supervisor and user can see the updated list. Don't try
to do undocumented work  -  visible task entries are how progress is tracked.

## What you cannot do

- **Cannot mark your own tasks done.** `nym_todo(status="done", ...)` is
  refused at the runtime layer for goal-locked items. Use `request_review`
  instead.
- **Cannot bypass the supervisor.** If the supervisor's verdict is REFINE,
  iterate; don't try to declare success without re-escalating.
- **Cannot delete or skip tasks unilaterally.** If a task no longer makes
  sense, raise it with the user and let them adjust the plan.

## Stop conditions

- **All tasks approved**: the goal auto-completes; report a summary to
  the user.
- **Stuck** (3+ consecutive refinements on the same task): the goal
  auto-pauses; surface the latest feedback to the user and ask how to
  proceed.
- **User pauses** (any message mid-loop): the goal pauses; respond to the
  user's message instead of escalating.
