# Input hint tips

Master list of the rotating tips shown under the prompt bar (the little `L`
connector on the bottom left). The live source of truth is the shared
EXACT_MATCH data file `src/lib/utils/inputTips.ts` (backlog #135): both apps'
`InputHintTips.svelte` components render from it, mobile filtering out the
`desktopOnly` entries (desktop chrome: sidebars, dashboard clicks, the
context dot), so the drift gate keeps the copy in sync. Keep this file in
sync when you add, reword, or remove a tip so we can track them all and
avoid duplicates.

## Style guide

- One short sentence, ending with a period.
- Plain, friendly, second person ("you") or "Nymeria" as the subject.
- Refer to features by their real names (slash commands like `/orchestrate`,
  tools like `tool_create` and `rag_search`).
- No em dashes.
- Keep it short. The tip shares its row with the "Ctrl + Enter to send" hint on
  the right, so anything much past ~56 characters gets clipped with an ellipsis.
  Aim for 56 characters or fewer.

## Tips

Each tip is listed once. The order here matches `InputHintTips.svelte`.

1. Type / to browse slash commands.
2. Use /skill <name> to load a skill for this thread.
3. Use /kit <name> to bind a Skill Kit and its tools.
4. Each thread keeps its own model, tools, and memory.
5. Switch the model for a thread from its settings.
6. Schedule a task and Nymeria will run it on its own.
7. Set up triggers to start threads from email, RSS, webhooks, and HTTP polls.
8. Give a thread its own custom instructions in settings.
9. Create callable threads to hand work to a sub-agent.
10. Click the dot to minimise the context usage above.
11. Click any Global Dashboard item to open its thread.
12. Collapse either sidebar to free up room.
13. Skill Kits hot-load tools into the agent instantly.
14. Nymeria helps providers cache prompts to cut costs.
15. Nymeria can build new tools with tool_create mid-turn.
16. Use @<title> to message another thread in place.
17. Enable rag_search so Nymeria recalls details on demand.
18. Set the compaction threshold by tokens, not percentage.
19. Start a new thread when you switch tasks.
20. Use cheap models per thread for repetitive tasks.
21. Toggle dreaming to let a thread manage its own work.
22. Group threads into a team to hide them from the rest.
23. Use /orchestrate to spin up and manage an agent swarm.

## Ideas / backlog

Tips considered but not shipped (kept here so we do not re-propose duplicates):

- "Drop files onto the message box to attach them." (Removed: already covered by
  the static "Paste or drag files to attach" hint on the right.)
- "Paste or drag images straight into the message box." (Removed for the same
  reason.)
