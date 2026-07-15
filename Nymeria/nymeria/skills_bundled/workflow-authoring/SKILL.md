---
name: workflow-authoring
description: Author, test, and publish nym-SDK workflows, saved Python routines
  that chain tools, one-shot LLM calls, and sub-agent threads with plain code, run
  unattended on schedules, triggers, and webhooks, and cost zero tokens unless a
  step needs AI. Load this to codify a recurring routine (a poll, a briefing, a
  cleanup) as a reusable workflow tool, or to add an approval checkpoint to one.
metadata:
  nymeria:
    required_tools:
      - tool_create
      - workflow_info
    tool_ttl: 2h
---

# Workflow Authoring

A workflow is a saved Python function written against the `nym.*` SDK. It runs
as CODE in a sandboxed subprocess, not as a prompted agent turn: deterministic
steps are free, and AI happens only where you call it. A published workflow is
itself a callable tool, and it can fire unattended (scheduled TODO, trigger,
webhook, REST) without waking any agent.

First decide the artifact:

- A recurring or multi-step ROUTINE with deterministic glue (poll, diff,
  compose, deliver) -> a workflow (this kit).
- One reusable API call or small pure helper -> `tool-management`.
- Tools plus operating instructions for future threads -> `skill-management`.
- A persistent conversational specialist -> a callable thread, not code.

## The nym surface

- `nym.tools.<name>(...)`: any tool you could call yourself, same role gates
  and credentials (resolved parent-side; workflow code never sees secrets).
- `nym.llm(prompt, schema=, model=)`: one-shot LLM, no thread. Defaults to
  the cheap fast tier. The safest, cheapest AI step.
- `nym.thread(prompt, id_or_title=, schema=, mode=)`: a full sub-agent turn,
  in an existing callable thread or a fresh spawn. Expensive; use when the
  step needs tools plus judgment, or to deliver into a conversation.
- `nym.threads.create(title, tools=, kit=, model=, ...)` /
  `nym.threads.configure(id_or_title, ...)`: define or adjust threads without
  running a turn. Create-then-invoke (create once, then `nym.thread` by id)
  is also the cancellable composition for sub-thread work.
- `nym.state.get/set/delete(key, ...)`: this workflow's persistent scratch
  (per user), for cross-run memory like "last seen hash". Not the user's
  memory store.
- `nym.approve(prompt, state, resume=)`: suspend for human approval (below).
- `nym.emit`, `nym.todo.add`, `nym.notify`, `nym.memory.add/read`: events,
  TODOs, delivery-profile notifications, real user memory.

Budget per run (overridable per workflow, admin-clamped): 50 verb calls, 10
AI calls, nesting depth 2, 600s wall clock (up to 3600s), 30KB per result.
Note the wall clock you declare is an upper bound, not a guarantee: a workflow
invoked as a TOOL is lowered to `tool_timeout - 2` (298s by default), and only
headless surfaces (recurring TODO, trigger, REST execute) keep the full
declared budget. Structured results are plain dicts (`triage["urgent"]`); pass
schemas as top-level kwargs only.

## Economics: nym.llm vs nym.thread

`nym.llm` is a stateless completion on the fast tier: use it for extraction,
classification, one-paragraph judgment. `nym.thread` spins a real agent turn
with tools and history: minutes and real money. Default to `nym.llm`; reach
for `nym.thread` only when the step genuinely needs tool use or must land in
a visible conversation. A workflow with neither costs zero tokens per fire.

## Authoring flow

Draft -> test -> publish via `tool_create`. Any user may author; EXECUTING a
revision needs admin approval (admins self-approve on save; non-admin saves
notify the admins). Editing source, entrypoint, or continuations resets
approval. Parameters are derived from the entrypoint's type-hinted signature.
A test IS a real run: side effects fire, so test deliberately.

```json
{
  "action": "draft",
  "implementation_type": "workflow",
  "tool_id": "site_watcher",
  "name": "Site Watcher",
  "description": "Poll a URL and notify me when its content changes.",
  "python_code": "def run(url: str, alert_after_failures: int = 3):\n    ...",
  "entrypoint": "run",
  "continuations": []
}
```

Then `tool_create(action="test", draft_id="site_watcher", sample_params={...})`
and `tool_create(action="publish", draft_id="site_watcher")`. Inspect runs and
approval states with `workflow_info` (list, show, log, approvals, pending).
An author-error envelope returns the traceback: read it, fix the draft, retest.

## Worked example: the zero-AI watcher

The canonical unattended shape: poll in code, remember in state, alert only
on real change, spend tokens never (or only on a real change).

```python
import hashlib


def state_key(prefix: str, url: str) -> str:
    # nym.state is namespaced by (workflow_id, user_id) ONLY, never by your
    # parameters. One workflow fired for two URLs shares one state document, so
    # fold any parameter you key on into the key yourself: otherwise the two
    # fires overwrite each other's digest and BOTH report a change every fire.
    return f"{prefix}:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


def run(url: str, alert_after_failures: int = 3):
    digest_key, failures_key = state_key("last_digest", url), state_key("fails", url)
    # fetch_url_nymeria signals failure by RETURNING "[Error]: ...", not by
    # raising, so detect failure on the return value (try/except is a backstop).
    try:
        page = nym.tools.fetch_url_nymeria(url=url)
        failed = isinstance(page, str) and page.startswith("[Error]")
    except Exception as e:
        page, failed = f"[Error]: {e}", True
    if failed:
        fails = (nym.state.get(failures_key) or 0) + 1
        nym.state.set(failures_key, fails)
        if fails == alert_after_failures:  # a blip never pages; a streak does
            nym.notify(f"Watcher cannot reach {url} ({fails} tries): {page}")
        return {"checked": False, "failures": fails}

    nym.state.set(failures_key, 0)
    # A stable content hash, NOT the builtin hash(): each fire runs in a fresh
    # subprocess where hash() of a str is salted per process, so it would never
    # match across runs and every check would look "changed".
    digest = hashlib.sha256(str(page).encode("utf-8")).hexdigest()
    previous = nym.state.get(digest_key)
    nym.state.set(digest_key, digest)
    if previous is None or previous == digest:
        return {"changed": False}  # nothing wakes, nothing spends

    nym.notify(f"{url} changed since the last check.")
    return {"changed": True}
```

Fire it every 30 minutes with a recurring TODO bound to the workflow
(`workflow_id` + `workflow_params` on the TODO), or from a trigger via the
`run_workflow` action (declare an `event: dict` parameter to receive the raw
trigger event). Note `fetch_url_nymeria` is an opt-in catalog tool: it must be
enabled on the thread the TODO runs in, since a workflow resolves tools against
its thread. When a change needs INTERPRETATION rather than a fixed message,
replace `nym.notify` with `nym.thread(prompt=..., id_or_title=...)` so an agent
reasons about the diff in a visible thread. This watcher ships as the bundled
`url_watcher` recipe (see "Install a bundled recipe"), so you can install and
schedule it without writing it.

## Worked example: a two-thread conversation

Two of your callable threads talking to each other: each thread's reply
becomes the other's next prompt. Orchestration is external and sequential
(one `nym.thread` ask at a time), so the ancestor-deadlock guard never
triggers. Stop on a content sentinel or a hard exchange cap.

Budget this shape deliberately, because the runner does not hand your script
its deadline and delivery only happens at the END: a run that overruns its
wall clock is SIGKILLed and the whole transcript dies with it, not just the
last reply. Each reply is one AI call, so a long conversation must raise
`max_ai_calls` above the engine default of 10.

The trap is the wall clock. **Your declared `wall_clock_seconds` is not what
you get on the tool path**: when a workflow runs as a TOOL (which is what
`install_template` and a normal thread call do), the engine lowers the run's
wall clock to `tool_timeout - 2` (298s by default) whatever the definition
declares, because the tool node is already bounding the call. Only headless
surfaces (a recurring TODO, a trigger, REST execute) keep the full declared
budget. So do not clamp your loop against the number you declared. Keep your
OWN clock and stop while there is still room to deliver:

```python
# budget={"wall_clock_seconds": 3600, "max_calls": 40, "max_ai_calls": 25}
import time

MAX_EXCHANGES = 20  # coarse backstop; the time budget is the real governor


def run(thread_a: str, thread_b: str, opening: str, max_exchanges: int = 6,
        end_marker: str = "[END CONVERSATION]",
        time_budget_seconds: int = 240):  # fits the 298s tool path; raise headless
    speakers = [thread_a, thread_b]
    transcript, message = [], opening
    started, slowest = time.monotonic(), 0.0
    for turn in range(min(max(1, max_exchanges), MAX_EXCHANGES)):
        # Stop BEFORE a reply you cannot afford; the slowest so far is the
        # estimate for the next. Overrunning costs the whole transcript.
        if transcript and (time.monotonic() - started) + slowest > time_budget_seconds:
            break
        speaker = speakers[turn % 2]
        reply_started = time.monotonic()
        reply = str(nym.thread(
            prompt=f"Reply with your next message only. To end, include "
                   f"{end_marker}.\n\nTheir message:\n{message}",
            id_or_title=speaker, mode="ask",
        ))
        slowest = max(slowest, time.monotonic() - reply_started)
        transcript.append(f"[{speaker}]: {reply}")
        if end_marker in reply:
            break
        message = reply
    nym.notify("Conversation:\n\n" + "\n\n".join(transcript))
    return {"replies": len(transcript), "transcript": transcript}
```

The general rule: any workflow whose useful output is delivered at the end and
whose runtime is measured in minutes either needs a self-imposed time budget
like this one, or belongs on a headless surface. Work that legitimately needs
more than `tool_timeout` should not be invoked as a tool at all.

Both threads must be your own callable threads (`nym.thread` enforces
ownership and the callable flag). This ships as the bundled
`two_thread_conversation` recipe.

## Install a bundled recipe

Nymeria ships example workflows you can install in one step instead of
authoring from scratch. List them, then install by id (admin only, because
install publishes an approved GLOBAL workflow tool):

```
workflow_info(action="templates")
tool_create(action="install_template", template_id="url_watcher")
```

Install self-approves the trusted bundled revision and enables the tool on
this thread. It is idempotent by tool id: installing an already-installed
recipe publishes nothing, returns the existing tool with `created: false`, and
still enables it on this thread, so it is the right call from any new thread
that wants the recipe. It never overwrites an existing tool: an id held by
another tool, or by an edited copy of the recipe, is a clear error instead.
After that the installed tool behaves like any workflow tool: run it, schedule
it on a recurring TODO, or bind it to a trigger.

## The approval checkpoint idiom

`nym.approve` suspends the run durably (days, across restarts); the owner
resolves it from the UI/REST, never an agent. Resolution runs the named
continuation `(state, decision)` in a fresh process, and a DECLINE also runs
it with `decision["approved"] == False`, so write both branches. Records
expire declined after 7 days. Declare every continuation in the draft's
`continuations` list.

```python
# email_search / email_archive are ILLUSTRATIVE names: discover the real
# tool names for the user's mail integration with tool_search first.
def run(older_than: str = "30d"):
    stale = nym.tools.email_search(query=f"is:unread older_than:{older_than}")
    nym.approve(
        prompt=f"Archive {len(stale)} unread emails?",
        state={"ids": [m["id"] for m in stale]},
        resume="do_archive",
    )  # does not return; the run ends here with needs_approval

def do_archive(state: dict, decision: dict):
    if not decision["approved"]:
        return {"archived": 0}
    for mid in state["ids"]:
        nym.tools.email_archive(message_id=mid)
    return {"archived": len(state["ids"])}
```

## Delivery is your job

No firing surface delivers output. A headless run that computes a briefing
and returns it has told nobody: deliver explicitly with `nym.notify` (user
notification), `nym.thread` (into a conversation), or a messaging tool. The
return value is for run records, chaining, and the agent caller.

## Safety

- A test run is a real run: confirm with the user before testing anything
  destructive, secret-touching, paid, or externally visible.
- Prefer pure-tool steps; add AI verbs only where judgment is required, and
  put a `nym.approve` gate before irreversible bulk actions.
- Workflows run as the calling user: role gates, the management-tool
  denylist, and credential scoping all apply; do not try to route around
  them.
- Approval resolution is human-only by design; never suggest an agent can
  approve a pending workflow.

## Not the right kit?

- One reusable API call or pure helper -> `tool-management`.
- Packaging instructions plus tools for reuse -> `skill-management`.
- A credential the workflow's tools need -> `credential-management`.
- Overall philosophy and artifact choice -> `Skill(name="self-improve")`.

## Output

Tell the user what the workflow does, its id, its approval state (pending
admin review vs live), how it will fire (schedule, trigger, on demand), and
what it will deliver where. If approval is pending, say who has to act.
