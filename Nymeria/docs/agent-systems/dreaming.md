# Dreaming: shadow self-reflection threads

Dreaming is Nymeria's opt-in self-reflection loop. A dream is one short, focused agent turn that runs in a disposable "shadow" thread on behalf of a parent thread. The dream agent reads the parent's recent conversation, memory, instructions, skills, and triggers, then consolidates, prunes, and tunes those surfaces. It never talks to the user; its final message is an audit summary.

## Enabling dreaming

Dreaming is off by default and enabled per thread:

- Per-thread: `PATCH /threads/{id}/config` with `{"dreaming": {"enabled": true}}`, or the desktop Dreaming config tab. `DreamingConfig` also carries per-thread gate overrides (`min_interval_hours`, `min_idle_minutes`, `min_turns_since_last`), a dream `model`, and per-thread `system_prompt` / `kickoff_prompt` overrides.
- Global defaults: `dream_default_min_interval_hours` (6), `dream_default_min_idle_minutes` (30), `dream_default_min_turns_since_last` (10), `dream_default_model` in settings. Blank per-thread values inherit these.
- Global prompt overrides: `GET/PUT /settings/dream-prompts` writes data-dir override files for the dream system prompt and kickoff template (the shipped package files are never mutated). The desktop settings panel embeds an editor for both.

## How a dream fires

Two trigger paths, both entering `invoke_dream`:

1. **Manual:** `POST /threads/{id}/dream`. Refused with 409 while the thread is mid-turn, with 409 when dreaming is not enabled (pass `{"force": true}` to bypass the opt-in), and with 400 when the target is itself a shadow thread. Manual runs bypass the scheduler's interval/idle/turn gates by design.
2. **Automatic:** `sweep_dreamable_threads` runs every 10 minutes. In the slim shape the in-process Ticker drives it; in the Docker stack an API-side heartbeat does (the worker runs no agent). A thread is eligible only when ALL gates pass: dreaming enabled, not itself a shadow, `min_interval_hours` since the last dream, at least `min_turns_since_last` user turns since the last dream, and at least `min_idle_minutes` of inactivity. Turn counts and the idle clock come from the parent's activity log, so a dream never resets its own gates. An owner guard dreams a thread only under its owning user, and a process-local single-flight slot prevents a manual and a scheduled dream from racing the same parent.

## What a dream does

- The shadow thread is created with the dream system prompt (replacing soul.md), a strict tool allowlist, and `shadow_parent_id` pointing at the parent. It is registered as a temporary thread (24h idle timeout) so the standard spawned-thread sweeper cleans it up.
- The parent's conversation is forked into the shadow (checkpoint clone plus soft-pruning of bulky tool results) so the dream reflects on what actually happened. This is best-effort: if the clone fails the dream still runs from memory.
- The dream turn then works through seven prompt phases: orient, reconcile memory, tweak instructions, curate skills, review triggers, schedule follow-ups, summarize. Every phase is evidence-anchored: the prompt forbids speculative writes, speculative TODOs, trigger creation without an explicit user ask, and deleting anything the user did not abandon.
- Progress streams as autonomous SSE events keyed on the shadow thread (`task_started`, chunks, `task_completed`), and a `dream_completed` sync event lands on the PARENT thread carrying the shadow id and the summary text.

## Parent retargeting

Inside a shadow thread, thread-scoped tools act on the parent, never on the shadow: memory and notepad tools, TODO tools, `thread_instructions_set`, skill enablement (`skill_manage` enable/disable/prune/status), and trigger binding (a trigger created during a dream binds to the parent; `trigger_info(current_thread_only=true)` filters by the parent). All of this rides one shared helper (`get_effective_thread_id`), so the contract cannot drift per tool family. A retargeted skill enable also skips the same-turn graph-reload dance: the parent picks the change up on its next turn.

## The tool allowlist

Dream shadows do not use the user's default tools. Graph construction short-circuits to a strict allowlist:

- Core (always bound): `memory_add`, `memory_edit`, `memory_read`, `nym_todo`, `nym_todo_delete`, `nym_todo_list`.
- Optional (dream policy set): `thread_instructions_set`, the skill family (`skill_write`, `skill_edit`, `skill_manage`, `list_installed_skills`, `search_skills`, `install_skill`), `tool_create`, and the trigger review pair (`trigger_config`, `trigger_info`).
- Explicitly excluded: bash, file tools, web search, notify, credential tools, slash dispatch, consult. Anything outside the policy set is dropped at graph build even if present in the shadow's config.

Note on `tool_create`: it binds for every role, but its admin controls live inside the tool itself (non-admin publishes create pending approval requests, and Python custom tools only execute with an admin-approved revision). The dream prompt deliberately does not direct tool authoring.

## Lifecycle hooks and dreams

Dream turns are ordinary autonomous turns to the hooks engine: PROMPT_SUBMIT, PRE/POST tool, and DONE hooks all fire, so guardrail hooks keep protecting autonomous work. Dreams are distinguishable everywhere the fire gate can see: `holder_kind` is `"dream"` and `trigger_label` is `Dream("<parent_thread_id>")`. To keep a noisy hook out of dreams, add a fire condition `holder_kind not_equals dream`; to scope a hook to dreams only, use `holder_kind equals dream`.

## Observability

- The desktop dashboard streams the dream as an autonomous task; the parent thread receives the `dream_completed` summary card.
- `DreamingConfig.last_dream_at` / `last_dream_thread_id` on the parent record the most recent cycle (these are also the scheduler's gate inputs).
- The shadow thread remains inspectable (history, config) until the idle sweeper removes it.
