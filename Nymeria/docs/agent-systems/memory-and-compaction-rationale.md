# Why Nymeria puts memory in the context window (not the system prompt)

This document explains the design choices behind Nymeria's persistent-memory and
context-compaction system, and why they differ from how most agent platforms
work. It is written to answer the question "why did you build it this way instead
of injecting memory into the system prompt like everyone else?"

The short answer: **prompt caching is prefix-based on every major provider, so
memory that changes often must live at the end of the context (the conversation
tail), not at the front (the system prompt).** Putting mutable memory in the
system prompt quietly destroys cache hit rates. Nymeria therefore surfaces memory
as an authentic `memory_read` tool exchange that lives in the conversation, and
compaction preserves that shape instead of wiping to a summary string.

For the mechanics, see `compaction-and-checkpoints.md`. This document is the
"why".

---

## The problem

An assistant with long-term memory has two facts in tension:

1. It needs durable knowledge (who the user is, project state, preferences) and a
   record of the current task available to the model on every turn.
2. The context window is finite and every token is re-sent (and re-priced) on
   every turn, so what you keep in context has a real, recurring cost.

Every agent platform solves (1) somehow and pays for (2) somehow. The usual
solution shapes the second cost in a way that is easy to miss.

## The mainstream approach: system-prompt injection

Most platforms inject retrieved memory/facts into the **system prompt**: the
profile, relevant memories, and sometimes a running summary are rendered into the
system message (or a leading developer message) on every request. It is simple,
it is one place, and the model reliably attends to the front of the context.

Nymeria itself does this for genuinely static context (persona, the current date,
tool descriptions). It is the right tool for content that does not change.

The trap is using it for content that **does** change, like user memory.

## Why we rejected it: prompt caching is prefix-based

Every major provider caches prompts by **prefix**, and the cache is invalidated
from the first byte that differs onward. This is not a Nymeria assumption; it is
stated in the providers' own documentation:

- **Anthropic.** Cache prefixes are created in the order `tools` -> `system` ->
  `messages`, forming a hierarchy where "changes at each level invalidate that
  level and all subsequent levels." Their invalidation table is explicit: a
  change to tool definitions invalidates the tools, system, **and** messages
  caches. A change to the system prompt invalidates system and messages.
- **OpenAI.** Automatic prefix caching: "cache hits are only possible for exact
  prefix matches." Anything after the first divergence is recomputed.
- **Google Gemini.** Implicit and explicit caching are both prefix-based; the
  guidance is to keep the stable content at the start and put variable content at
  the end.
- **OpenRouter.** Passes `cache_control` through to the underlying provider and
  otherwise inherits the provider's implicit prefix caching. No independent
  segmentation.

The consequence is the crux of the whole design:

> The system prompt sits at the very front of the prompt. If you put memory there
> and the memory changes, you invalidate the cache for the **entire** request from
> that point on, including the whole conversation history. The next turn re-bills
> the full transcript at the uncached rate.

User memory changes often: the agent saves a new fact, updates the notepad, edits
a preference. With memory in the system prompt, each of those edits silently busts
the full-transcript cache on the next turn. On a long thread that is the most
expensive thing you can do.

Memory placed in the **conversation tail** has the opposite property. Updates are
**append-only**: writing a new memory adds a new tool exchange at the end. The
cached prefix (everything before it) stays byte-identical and stays cached; only
the new tokens are prefilled. Mutating the tail never invalidates the history
prefix the way mutating the front does.

So the placement decision is not aesthetic. For frequently-changing memory, the
tail is cache-correct and the system prompt is cache-pessimal.

(There is a secondary, softer argument too: a system prompt is conventionally
"how the model should behave," and overloading it with volatile facts blurs that.
We find this real but minor. The decisive factor is caching.)

### A related caching note: dynamic tool binding

Because `tools` sit at the very front of the prefix (ahead of even the system
prompt), changing the bound tool set mid-thread (skill-kit TTL expiry, tool
hot-loading, per-thread binding changes) invalidates the entire cache. This is a
separate cost from memory placement, but it is the same root cause: anything you
mutate near the front is expensive. Keeping the per-thread tool set stable across
a thread's turns is worth doing for the same reason.

---

## What Nymeria does instead

Memory is surfaced as an **authentic `memory_read` tool exchange that lives in the
conversation history**, near the start of the thread (and re-established after
compaction). Concretely:

```
HumanMessage (internal)   "loading / resuming your memory"
AIMessage    tool_calls=[ memory_read(global), memory_read(thread) ]
ToolMessage  <real global profile contents>
ToolMessage  <real thread notepad contents>
```

Threads in a callable team carry a third `memory_read(scope="team")` call and
ToolMessage in the same exchange (backlog #100 phase 3): the shared team
key-value registry plus the team's identity header (name, description,
teammate roster). Unteamed threads keep the two-read shape; a thread that
joins a team mid-thread is deliberately NOT reseeded (that would bust the
warm cache) and picks the team block up at its next compaction or explicit
read.

This is the same shape the agent would produce if it read its own memory, and in
practice the model treats it exactly that way (its own reasoning refers to "the
memory I just loaded"). Three properties matter:

1. **It is in the cache-stable tail**, so it does not bust the prefix when memory
   changes (memory changes append a new read elsewhere, they do not rewrite this
   block).
2. **It is authentic**, not a fabricated "here are some facts" block. The content
   is the real output of the memory tools, so there is no novel format for the
   model to learn and no risk of it distrusting injected text.
3. **It is provider-neutral.** Tool calls are stored in LangChain's canonical form
   and re-serialized per provider at request time, so the exact same exchange
   works across Anthropic, OpenAI/Codex, and OpenRouter, including switching
   provider mid-thread.

This is delivered in three pieces.

### 1. Fresh-thread seed

When a brand-new thread takes its first turn, Nymeria seeds the memory-read
exchange into the checkpoint before the first user message. The agent therefore
starts every conversation already knowing its memory, deterministically, instead
of relying on it to remember to call `memory_read` (which it did only
sometimes). The seed is hidden from the user-facing transcript but is real context
for the model.

### 2. Compaction that retains a resume turn (not a summary string)

When context fills up, the obvious approach is: summarize, wipe the messages, and
glue the summary string onto the next user message. We did that originally and
replaced it.

Instead, compaction now:

- runs a real compaction turn where the agent persists durable facts to global
  memory and working state (plus key file paths) to the thread notepad, and emits
  a structured summary;
- discards that turn's messages and rebuilds the thread to a small **retained
  tail**: a "[Session resume] ... <summary> ..." opener, then a fresh
  `memory_read` exchange carrying the **post-edit** memory.

Why this shape:

- **Authenticity + naturalness.** The carried context is a real summary plus a
  real memory read, in the same shape as a normal turn, rather than a synthetic
  string spliced onto the user's message.
- **Automatic restart recovery.** The carried context is just persisted messages
  in the checkpoint. There is no in-memory "pending summary" to lose on a process
  restart, so the recovery machinery that used to exist is gone.
- **The read-back is injected by the framework, deterministically, after the
  agent's writes.** We do not rely on the agent choosing to call `memory_read`
  during compaction (unreliable), and it is read after the writes so it is never
  stale. It is also robust if memory tools are ever disabled: the prompt only asks
  the agent to write and summarize, never to read.
- **No duplication.** We do not keep the agent's own compaction-turn reads; we
  keep only its summary and its writes (which are side effects on the files), then
  inject exactly one authoritative read-back.

### 3. Sub-turn compaction with seamless resume

Compaction used to be able to fire only at turn boundaries. That blocks a
multi-step task: an agent in the middle of a tool loop would either blow past the
limit or be cut off at the end of the turn.

Nymeria checks the running context size **after each tool batch** (reading the
latest response's real input-token count, since the per-turn token tracker is
stale mid-loop). If it crosses the trigger, the graph halts at that sub-turn
boundary, compaction runs, and the graph is re-driven so the agent continues from
the reloaded memory. Because the retained tail ends on the memory-read results
(with no trailing assistant message), "resuming" is just re-entering the graph;
no re-prompt is needed.

A deliberate subtlety: we only force a resume when we **interrupted** the agent
mid-loop. A sub-turn boundary is always a genuine mid-task point (the agent has a
pending model call to process the tool results it just received). When the agent
instead ends a turn on its own (a final response with no tool calls), that is the
agent signaling it is **done**, and we do not force it to continue. So a finished
agent is never artificially resumed.

---

## Approaches we considered and rejected

| Strategy | Why we did not use it |
| --- | --- |
| Memory in the system prompt | Front-of-prefix; mutating it busts the whole-transcript cache. Memory mutates often. (The mainstream choice; the caching cost is usually unmeasured.) |
| Dumping a raw facts block into context | Synthetic, unnatural format; the model treats a real tool result more reliably than an injected "here are facts" blob. |
| Relying on the agent to call `memory_read` itself | Nondeterministic; it only sometimes did it on fresh threads. The seed makes it guaranteed. |
| Compaction = wipe + summary string on next user message | Loses authenticity, needs fragile in-memory pending-summary state with restart recovery, and the summary alone drifts from current memory. |
| Forcing the agent to read memory during compaction (prompt instruction) | Unreliable, and breaks if memory tools are disabled. The framework injects the read-back deterministically instead. |
| Re-driving after a normal (turn-end) compaction | Would force a finished agent to keep going. We only resume after a mid-loop interruption. |
| A dedicated "memory role" separate from system/user/assistant | Does not exist in current provider APIs. The closest is a clearly-delimited region, but the tail is both cache-correct and natural, so we use it. |

## Honest trade-offs

- **Tokens.** Memory in the tail is re-sent every turn, same as a system-prompt
  block would be. The win is caching: a stable tail bills at the cached rate.
  On-demand reads still let the agent pull more detail only when needed.
- **One extra read exchange after compaction.** Small and bounded by the memory
  size caps; it is exactly the context worth carrying.
- **The seed/retained exchange is synthetic-but-authentic.** It is constructed by
  the framework, but its content is real tool output and its structure is a normal
  tool call, which providers and the model both accept as ordinary history.

## Summary

Most agent platforms inject memory into the system prompt because it is simple.
That choice is quietly cache-hostile for anything that changes, because all major
providers cache by prefix and the system prompt is at the front. Nymeria keeps
memory in the cache-stable conversation tail as an authentic tool exchange, seeds
it on fresh threads, and preserves it across compaction as a retained resume turn
that also lets a mid-task agent continue seamlessly. The result is memory that is
always present, always fresh, provider-portable, and cheap to keep in context.
