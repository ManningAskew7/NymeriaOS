# Nymeria UI Knowledge Base

**Generated:** 2026-04-21 · **Updated:** 2026-05-13 · **Source commit:** `e715093`

---

## Reader-LLM Preamble

You are reading this document because a Nymeria end-user pasted it into you so they can ask questions about how to use the product. This file is a complete reference for every user-facing surface of the Nymeria platform: the desktop app, the mobile app, the Discord and Telegram bots, the Outlook add-in, the interactive CLI, and the REST/SSE API.

**Rules of engagement when answering:**

1. **Answer strictly from this document.** If a feature is not described here, say so  -  do not invent commands, menu items, or keyboard shortcuts.
2. **Assume the asker is a non-developer** unless context says otherwise. Translate technical detail into step-by-step instructions ("click the gear icon in the bottom left, then select the LLM tab…").
3. **Cross-platform questions**  -  when a user does not say which client they are on, ask. The same capability is often exposed differently on desktop vs mobile vs Discord.
4. **Admin/deployment questions** are out of scope. If asked how to set up a Docker container, deploy the backend, or issue API keys, direct the user to `Nymeria/docs/` (PRODUCTION_DEPLOYMENT.md, QUICKSTART.md, configuration.md) and say that their administrator handles those.
5. **One content caveat carries an explicit warning in this doc; relay it to the user when relevant:**
   - ⚠️ **Twitch** integration is in flux: the standalone Twitch bot has been removed, and the Twitch tools are currently disabled pending a rewrite to standalone optional tools. Treat any Twitch capability as unavailable for now.

---

## 1. Platform Overview

Nymeria is a personal AI assistant. A single backend ("the API") runs a LangGraph ReAct agent with persistent threads, autonomous scheduling, tools, skills, memories, and TODOs. A family of thin clients lets the user talk to that backend from wherever they are:

| Surface | Technology | Primary use |
|---------|-----------|-------------|
| Desktop app | Tauri 2 + Svelte 5 | Full-featured workstation client. All features available. |
| Mobile app | Capacitor 6 + Svelte 5 | Pared-down on-the-go client. Swipe-navigated panels, haptics, camera attach. |
| Discord bot | discord.py slash commands | Chat with Nymeria from any Discord channel or DM, per-channel threads. |
| Telegram bot | python-telegram-bot | Chat with Nymeria from Telegram DMs or groups, per-chat threads. |
| Twitch bot (⚠️ transitional) | TwitchIO | Chat moderator + responder for Twitch chat. Being refactored. |
| Outlook add-in | Office.js taskpane | Sidebar inside Outlook that knows which email you are viewing. |
| Interactive CLI | prompt_toolkit + Rich/plain renderers | Full-screen terminal client and shell fallback for power users. |
| REST / SSE API | FastAPI | Programmatic surface. All UIs are clients of this. Also exposed to webhooks, automation clients, MCP. |

**Vocabulary that appears throughout the product:**

- **Thread**  -  one conversation with Nymeria. Each thread has its own message history, its own optional LLM override, its own tool and skill config, its own TODOs. Threads never mix context.
- **Trigger**  -  a rule that fires on an event (cron, webhook, RSS, etc.) and starts an autonomous agent run.
- **TODO** (called "Task" in the frontend UI)  -  a scheduled prompt. When the scheduled time hits, the agent runs the prompt and delivers the result back to its originating client.
- **Skill**  -  a bundled `SKILL.md` + scripts + references. Only the name/description loads into context by default; the agent pulls the full body on demand.
- **Notepad**  -  per-thread free-form text that survives context compaction. Lives outside the rolling context window.
- **Memory**  -  per-user persistent key/value facts the agent can look up across any thread.
- **Compaction**  -  when the context window fills, the backend summarizes older turns and replaces them with the summary, freeing tokens.
- **Autonomous run**  -  any agent run not initiated by a live chat turn: TODO firing, trigger firing, watchdog nudging a stale task. Results stream over `/autonomous/stream` and are pushed to the originating client.

---

## 2. Desktop App

**File location of UI:** `nymeria-desktop/src/lib/components/`. Entry route: `src/routes/+page.svelte`. The shell is `components/layout/AppShell.svelte`.

### 2.1 Shell & layout

Three fixed panels in a flexbox row:

| Panel | Default width | Collapse target | Toggle |
|-------|---------------|-----------------|--------|
| Left sidebar | 280 px | 64 px (icon-only) | `Ctrl`/`Cmd` + `B`, or hover-arrow on sidebar's right edge |
| Center (chat) | fills remaining |  -  |  -  |
| Right panel (Dashboard) | 320 px | 0 px (hidden) | `Ctrl`/`Cmd` + `Shift` + `B`, or hover-arrow on panel's left edge |

**Window constraints (Tauri):** initial 1400 × 900, minimum 1000 × 600.

**Shortcut suppression:** the global `Ctrl+B`/`Ctrl+Shift+B` shortcuts are ignored while the cursor is inside an input, textarea, or contenteditable  -  typing `B` in a message does not toggle the sidebar.

**Outlook mode exception:** when the desktop app is embedded in the Outlook taskpane (`?outlook=1`), only one side panel may be open at a time  -  opening the right panel auto-closes the sidebar and vice versa, because the taskpane is too narrow for three columns.

### 2.2 Sidebar (left panel)

Top to bottom: logo + "New Thread" button, thread list, notifications/connection/settings footer.

**"New Thread" button:** creates an empty thread on the backend, selects it, clears the chat area. In collapsed mode shrinks to a `+` icon.

**Thread list (`components/threads/ThreadList.svelte`):**

- **Sort modes** (picker at top of the list): `recent` (default, grouped by Today/Yesterday/Last Week/Older), `oldest`, `alphabetical`, `tasks` (most active TODOs first), `active` (activity in last 24 h first).
- **Folders:** threads can be dragged into folders. Folders are collapsible, draggable to reorder. Pinned folders group related threads.
- **Platform badges:** each thread row shows a small icon indicating origin  -  Discord, Telegram, Slack, Trigger, Callable, or plain chat (desktop).
- **Multi-select:**
  - `Ctrl`/`Cmd` + click  -  toggle individual thread into the selection.
  - `Shift` + click  -  range-select from last clicked to target.
  - Plain click clears the selection.
  - Selected threads get a highlight. Bulk-action buttons appear: **Bulk Delete** (with confirm modal) and **Bulk Group** (move all to a folder via folder picker).
- **Rename:** double-click the title, edit inline, press `Enter` to save or `Escape` to cancel. Blur saves after a 150 ms delay.
- **Right-click menu:** opens at cursor with `Pin / Unpin`, `Configure`, `Rename`, `Copy ID`, `Delete`. Same menu on folder right-click.
- **Agent shortcut:** on desktop, hovering/focusing a thread row shows an Agent settings shortcut that opens that thread's settings panel directly to the Agent tab.
- **Badges on the row:** task count (if any), custom-config indicator, callable-thread indicator, pin indicator.

**Sidebar footer:**

- **Notifications bell**  -  opens the Notification Center popover. Shows an unread-count badge with a glow animation.
- **Account badge**  -  shows the current account's avatar (initials in a deterministic colour), display name, and a small role chip (`admin` / `user`). Click to open the Account Menu (see §2.13).
- **Settings gear**  -  opens the Settings modal (see §2.6).

### 2.3 Chat area (center panel)

Layout top-to-bottom: Thread Header → optional Outlook Quick Actions bar → Chat Container → optional Context Status Bar → Input Bar.

**Thread Header (`components/layout/ThreadHeader.svelte`):** shows thread title, platform indicator, active model, and a `Configure` button that opens the Thread Settings panel (see §2.5). Badges summarize the active model, non-MCP tools, MCP server tools, callable threads, skills, triggers, instructions, and callable-thread status.

**Chat Container:** scrollable message history.
- Empty state: "No messages yet. Start a conversation or select a thread."
- Loading state: spinner + "Loading conversation history…".
- Messages render through `components/chat/MessageBubble.svelte`:
  - **User messages**  -  right-aligned, lighter background. Attachments render as thumbnails (images) or file cards (documents). If the message has a context-summary suffix (from `/compact`), it renders as a collapsible "Context Summary" sub-section. Autonomous wake-up messages, system compaction-request messages, and AI compaction-summary messages are filtered out of the display.
  - **Assistant messages**  -  left-aligned, semi-transparent background. Content renders in the order it streamed: thinking, visible response text, tool calls, and post-tool response text.
    - **Thinking block**  -  collapsible "Thinking…" header; click expands and shows the reasoning as markdown.
    - **Tool-call card**  -  tool name header with status badge (pending / success / error); arguments JSON expandable; result text expandable if long. Next to the tool name, a short plain-English summary of the call is shown as a soft italic aside so the collapsed card is readable without expanding it. It is derived deterministically and client-side from the call's own arguments: the value of a self-describing argument (a search query, shell command, agent task, path), a named target prefixed by the action verb, or the operation plus the entity type it acts on. Labels that would only restate the tool name are omitted. Toggled by the "Describe tool calls" setting (Appearance, on by default). If the tool produced a workspace artifact (`file_write(..., attach=True)`), a "View artifact" button opens a modal that previews the file (code highlighted, images inline, others as raw text) and offers copy/download.
    - **Response**  -  markdown-rendered. Skill invocations appear inline as clickable cards; clicking them expands to show the skill's contribution.

**Context Status Bar (`components/chat/ContextStatusBar.svelte`):** shows current token usage, usage percent of the context limit, active model, compaction status. Only visible when the thread has context stats.

**Input Bar (`components/chat/InputBar.svelte`):**
- Textarea auto-resizes up to 200 px.
- Placeholder changes by state: "Type a message…" / "Waiting for response…" (while streaming) / "Waiting for autonomous task to finish…" (while an autonomous run is queued).
- Send with `Ctrl`/`Cmd` + `Enter`. Plain `Enter` inserts a newline.
- **Send/Stop toggle:** the send button flips to "Stop" during streaming; clicking aborts the current run.
- **Attachments:**
  - Drag-and-drop a file onto the chat area (overlay appears while dragging).
  - Click the paperclip to open a file picker.
  - Paste from clipboard.
  - Max 10 files per message. Validated types include common images (jpg/png/gif/webp), documents (pdf/txt/md/csv/json/xml), and source code extensions.
- **Attachment compatibility check:** before sending, the frontend asks the backend whether the current model supports the attached modalities. If not, a warning modal lists incompatibilities and offers "Send anyway" (red button). A "Don't show this warning again" checkbox suppresses the modal for future sends.
- **Hint:** "Press Ctrl+Enter to send" displayed below the textarea.
- **Slash commands:** type `/compact` (and any other command the backend exposes) as the entire message text and hit send  -  the backend routes it as a command, the response comes back as a `command_result` SSE event. There is no autocomplete or slash-menu in the frontend.
- **Cross-thread dispatch:** prefix a message with `@ThreadName`, `@CallableName`, or `@"Thread With Spaces"` to route that turn to another thread. The routed response streams in the current view with a `Response from <thread>` reference line inside the normal assistant message, while preserving thinking, preamble, tool-call, tool-result, artifact, and final response rendering. The target thread owns the persisted conversation history.
- **Voice:** no voice-record button exists in the chat. TTS/STT can be configured (see §2.6 LLM/Voice settings) but voice interaction is currently only available programmatically through `/voice/*` endpoints.

### 2.4 Dashboard (right panel)

Two tabs at the top: **This Thread** and **Global**. The tab auto-switches to Global when no thread is selected.

Three sections stacked vertically, each collapsible (all open by default except Activity which is always-visible).

**2.4.1 Tasks (TODOs)**  -  `components/todos/TodoFeed.svelte`

Three sections, in this fixed order. Each renders only if it has items:

| Section | What's in it | Visual treatment |
|---------|--------------|------------------|
| **IN PROGRESS** | Tasks the agent has started but not finished | Highlighted card: accent-primary tint, accent badge, `highlighted` flag on each row |
| **UPCOMING** | Pending tasks (sorted: soonest scheduled first, then by creation time) | Normal card, muted count badge |
| **COMPLETED** | Done tasks (sorted: newest completion first) | Normal card, scrollable with max-height 300 px |

The Completed section shows all completed tasks (no 7-day cutoff on the frontend; any cutoff is server-side).

**"Global" tab** shows the same three sections but across all threads; each row displays its parent thread's title as a click-to-navigate link.

The task feed refreshes after live chat or autonomous SSE `tool_result` events
from TODO tools such as `nym_todo`, `nym_todo_delete`, legacy `todo_*` names,
and Nymeria MCP TODO tool names. The refresh preserves the currently visible
Dashboard scope, so **This Thread** stays thread-filtered and **Global** stays
global after the agent changes its own TODOs.

**The Add Task form** (`components/todos/TodoForm.svelte`)  -  opens as a modal, title switches to "Edit Task" in edit mode:

| Field | Type | Required | Details |
|-------|------|:--:|---------|
| **Task** | text | ✅ | Placeholder "What needs to be done?", max 500 chars. Error "Task description is required" on empty submit. |
| **Notes** | textarea |  -  | Placeholder "Additional details…", 3 rows, max 1000 chars. Plain text (not markdown). |
| **Schedule** | `datetime-local` |  -  | Native browser date+time picker. Stored as ISO UTC. When set, the thread selector below becomes visible. |
| **Repeat** | dropdown |  -  | Exact option values: `""` (Never  -  default), `5min`, `10min`, `15min`, `30min`, `hourly`, `daily`, `weekly`, `monthly`. No free-form schedules. |
| **Output to conversation** | dropdown |  -  | Only appears when Schedule is filled. Options: `+ New conversation` (creates a thread titled "Scheduled: {task preview}…") or one of the user's existing threads. Determines where the autonomous run's output will be posted. |

**Form buttons:**
- Cancel (secondary).
- Submit ("Create Task" / "Save Changes" based on mode). Disabled while saving or deleting.
- In edit mode only: a Delete button (trash icon, ghost style). Click reveals an inline confirm ("Delete this task?" → Yes, Delete / Cancel).

**TodoItem row** (`components/todos/TodoItem.svelte`):
- **Left:** 16 px checkbox. Click marks the task done; during the request a small spinner replaces the checkbox and the row is disabled. On completion a check icon bounces in. Recurring tasks auto-reschedule server-side (the item either moves to Completed and a new one appears, or the same row updates its next-run countdown).
- **Middle:**
  - Creator icon (tiny user icon) if `createdBy === 'user'`.
  - Task text. Strikethrough + dim when completed. Single line, ellipsis.
  - Chevron appears if notes exist; click the row to expand and show the notes block.
  - Metadata row below the title, separated by centered dots: thread name (clickable to navigate in Global view), recurrence label ("Every 5m", "Daily", …), and **live countdown** that updates every second (`2d 5h`, `1h 42m`, `45s`, "Due now").
- **Right:** Edit pencil (hover-visible) reopens the form.

**Scheduled rows** get a thin accent-primary vertical rail on the left edge.

Status values: `pending` → Upcoming, `in_progress` → In Progress, `completed` → Completed. There is no "priority" field exposed in the form.

**2.4.2 Triggers**  -  `components/triggers/TriggerFeed.svelte`

Two groups, each with a count badge:

- **ACTIVE** (enabled triggers)  -  highlighted container (accent-secondary tint), sorted to top.
- **PAUSED** (disabled triggers)  -  normal container.

**Per trigger row** (`TriggerItem.svelte`):
- Source icon + trigger name. Disabled triggers show strikethrough.
- Health dot (6 px): green `healthy` / yellow `degraded` / red `failing`. Failing triggers also show a one-line error hint below.
- Meta badges: source type, action type, fire count (`{n}x`), last fired ("Just now" / "{m}m ago" / "{h}h ago" / "{d}d ago" / "Never").
- Thread badge (if thread-scoped)  -  clickable to jump to the thread.
- Enable/disable toggle switch.
- **Test button** (terminal icon)  -  calls the test endpoint; a brief accent-colored preview of the test result appears for ~5 s.
- **History button** (clock icon)  -  opens the Trigger History panel.
- **Edit button** (pencil icon)  -  reopens the wizard in edit mode.
- **Delete button** (X icon)  -  two-click confirm: first click reveals a checkmark + cancel pair; second click deletes.

#### The Trigger Setup Wizard (step-by-step)

Opened from the `New Trigger` button in the Dashboard's Triggers section (or from inside a thread's Settings → Triggers tab). It is a 5-step modal; a progress bar at the top lets the user jump back to any already-completed step. Step labels: **Source → Config → Filters → Action → Review**.

**Step 1  -  Source**
- Search input ("Search sources…") and category tabs: **All**, plus any of `communication`, `monitoring`, `developer`, `custom`, `general` (whichever categories have sources registered).
- Each source is a clickable card showing an icon, its human name (underscores stripped), a short description, and an "auth" badge if the source requires the user to authenticate first (e.g. Outlook, Discord).
- Picking a source pre-fills the next step with that source's `example_config` and any default values.
- Next is disabled until a source is selected.

**Step 2  -  Configure**
- Shows an optional setup-guide box at the top if the source provides one (e.g. "Paste your webhook URL into service X").
- Fields are generated from the source's schema: **text**, **number**, **password** (for secrets), **checkbox** (booleans), or **dropdown** (when the schema declares an `enum`).
- Fields are grouped ("General", "Advanced") and sorted within each group.
- Required fields show `*` in the label; trying to advance with a required field blank produces the error `"{description} is required"`.
- Examples of what shows up per source: a cron source shows a cron expression field; a webhook source shows the URL to POST to and an optional shared-secret field; an Outlook source shows folder / filter fields.

**Step 3  -  Filters (conditions)**
- Optional. Empty state message: "No conditions  -  trigger fires on every event".
- **Skip** button advances without adding any.
- Each condition row:
  - **Field** dropdown  -  populated from the source's template variables (e.g. `title`, `sender`, `body`).
  - **Operator** dropdown  -  exactly five operators: `contains`, `equals`, `not_equals`, `starts_with`, `matches_regex`.
  - **Value** text input.
  - Remove button (X).
- Conditions combine with **AND**. There is no OR / group nesting in the UI.
- Add more with the `+ Add condition` button.

**Step 4  -  Action**
- Three mutually-exclusive action types:

  | Action | Required config field | Purpose |
  |--------|----------------------|---------|
  | **Agent Prompt** | `prompt_template` (textarea) | Run the agent with the rendered prompt. LLM will reply into the trigger's thread. |
  | **Notify** | `message_template` (textarea), optional `platform` text field | Push a notification; no LLM call. `platform` picks the delivery target (`desktop`, `discord`, `telegram`, …). |
  | **Create Task** | `task_template` (textarea) | Create a TODO from the event; no LLM call. |

- Below the template field, the wizard lists **Available variables**  -  the source's event fields rendered as monospace chips. The user types them into the template as `{variable_name}` (curly braces, exact name). Example placeholder: `New {sourceInfo.name} event: {content}`.

**Step 5  -  Review**
- **Trigger name**  -  text input, required. If left blank the placeholder suggests `"{Source} → {action}"`.
- **Cooldown (seconds)**  -  number input, min 0. `0` means no cooldown. Units are **seconds**, not minutes.
- **Enable immediately**  -  checkbox, default on.
- Read-only summary panel showing: Source, Action, filter count (if any), thread (first 12 chars of the ID, if thread-scoped).
- Save button label is "Create Trigger" (new) or "Save Changes" (edit).

**Back button** is available on every step except step 1, and the wizard always saves on the Review step's primary button. There is no partial-save  -  closing the modal before Review loses all entered data.

#### Trigger History panel

Opens from the clock-icon button on a trigger row. Shows a scrollable list of past executions, each a collapsible row:

- **Summary:** status dot (`success` green / `error` red / `deferred` muted / `partial` warning), time (`Just now` / relative minutes/hours / `MMM D, HH:MM` for older), event count (if > 1), duration in seconds, action type.
- **Expanded:** `events_summary` (the raw event payload preview), `response_summary` (what the agent / action produced), and an error block if the run failed. The footer shows the execution ID and the full localized timestamp.

#### Triggers tab inside a Thread

Inside **Thread Settings → Triggers** (see §2.5) the interface is a compact inline form rather than the full-screen wizard  -  intended for scoping a trigger to this specific thread:
- Same field set as the wizard but laid out as one scrollable form: Name (max 200 chars) → Source Type dropdown → dynamic Source Config fields → Action Type (`agent_prompt` / `notify` / `create_todo`) → Action Config → Cooldown (seconds).
- The scheduled-for field is **not** here (that belongs to TODOs, not triggers).
- Recurrence is **not** here either (same reason).
- Validation errors surface as red banners above the footer; Save is disabled while the form has errors.

**2.4.3 Activity**  -  `components/activity/ActivityFeed.svelte`
- Chronological log: tool executions, task completions, errors, message-count updates.
- "This Thread" scope vs "Global" scope (shows originating thread in Global).

**Below the dashboard:** **Connection Status** dot (green = healthy, red = disconnected) with the current API URL.

### 2.5 Thread Settings panel

Opened from the Thread Header `Configure` button, from the thread right-click menu, or from a desktop thread row's Agent shortcut. Modal with tabs across the top.

| Tab | What it configures |
|-----|-------------------|
| **Instructions** | A textarea for thread-specific custom instructions (appended to the system prompt for this thread). |
| **System Prompt** | Full system-prompt override for this thread. Option to reset to the global default. |
| **Agent** | Callable-thread registration: `Make Callable`, callable name, and callable description. |
| **Model** | Override provider / model / temperature / base URL / max tokens / extended thinking for this thread only. Leave blank to inherit. |
| **Tools** | Toggle non-MCP core tools on/off and non-MCP optional tools on/off. Performance warning modal appears if the total enabled tool count goes over 25. |
| **MCP** | Enable/disable MCP-discovered tools for this thread. Default MCP tools can be disabled here; non-default MCP tools can be enabled here. |
| **Skills** | Per-skill "Enable in this thread" checkbox. Overrides the global skill toggle just for this thread. |
| **Triggers** | List triggers scoped to this thread. Edit / Delete / History per row. Add-new button opens the same wizard as in the Dashboard. |
| **Chat App** | Telegram binding controls plus attention settings: autonomous Telegram delivery (`Full output`, `Notify only`, `Off`) and notification-center behavior (`Notify only`, `All autonomous completions`, `Off`). |

### 2.6 Settings (global, gear icon)

Opens a modal with tabs. Mobile uses the same tabs in a full-screen layout; see §3 for mobile differences.

**2.6.1 Connection**
- API URL (text).
- API Key (password with show/hide).
- **Test Connection** button  -  runs a health check, surfaces "Connected successfully!" or a specific error (bad key / network / etc.).
- **Saved Connections** (desktop only)  -  named profiles; switch, rename, delete. "Save Connection" button prompts for a name.
- **Advanced** (desktop only, collapsed by default on Tauri builds, expanded by default elsewhere)  -  LLM provider selector, custom base URL. Usually you change these from the LLM tab instead.

**2.6.2 Appearance**
- Theme picker (grid of cards with preview colors): Midnight (default dark), Light (warm paper), Platinum (monochrome dark). Theme IDs live in `nymeria-desktop/src/lib/themes.ts`.
- Font size slider (if your build exposes it).

**2.6.3 LLM**
- **Provider:** Anthropic (Subscription via CLIProxy), Anthropic (Direct API), OpenAI, OpenRouter, Local OpenAI. The frontend decides between "Subscription" and "Direct" by whether a custom base URL is set.
- **Model:** dropdown populated from the provider's `/v1/models` list. Shows the model name and context window if known. Default `claude-sonnet-4-6`.
- **Temperature:** 0–2 slider, default 1.
- **Base URL:** auto-populated for local/proxy modes.
- **Advanced (collapsible):** max tokens, top-p, top-k, frequency penalty, presence penalty, reasoning effort (for reasoning models), extended thinking toggle, "use model defaults" checkbox.
- **Model Help** button (info icon) opens a step-by-step guide: estimated cost per 1M tokens, context window, capabilities (vision, tools, extended thinking, …).

**2.6.4 Agent**
- **Context management:** `auto_compact` (summarize older turns when full) or `sliding_window` (drop oldest turns).
- **Sliding-window cycles** (default 5).
- **Max self-invokes per hour** (default 50)  -  how often the agent may re-invoke itself (watchdog, self-chained tools).
- **Log level:** DEBUG / INFO / WARNING / ERROR.
- **Watchdog**  -  enable toggle, interval in minutes (default 5), TODO staleness threshold in minutes (default 20).

**2.6.5 Tools**  -  see §9 for the underlying concept.
- Search field.
- **Core tools** list  -  toggle each non-MCP default tool on/off.
- **Available tools** list (i.e. non-MCP optional tools not in the core set)  -  toggle to add to the default set.
- **Custom HTTP tools** sub-section (desktop only)  -  list, Create (opens `ToolForm.svelte`), Edit, Test (opens `ToolTestPanel.svelte`), Delete, Enable/Disable.
- **Performance warning** appears over 25 total tools.
- **Save** button persists the tool set server-side.

#### 2.6.5a MCP

The dedicated **Settings → MCP** tab contains the MCP Servers panel. Desktop uses a pinned **Save Changes** footer for discovered MCP default-tool toggles; mobile saves discovered-tool toggles directly from each server row. Enabled discovered MCP tools are saved into the default core tool set inherited by new threads.

> ℹ️ **Current scope caveat.** The Add/Edit form only supports stdio-transport servers (`command` + `args` + `env`). There is no transport-type selector; SSE, HTTP, and WebSocket MCP servers have no dedicated UI today. Per-tool toggles in Settings → MCP control the default tool set; per-thread overrides live in Thread Settings → MCP.

**The Add Server form** (`components/tools/MCPServerForm.svelte`):

Two preset buttons appear **only in Add mode** and auto-fill the command and args:
- **Filesystem**  -  `npx -y @modelcontextprotocol/server-filesystem /path/to/dir` (replace the path with the directory you want the server to expose).
- **Fetch**  -  `npx -y @modelcontextprotocol/server-fetch` (HTTP fetch tool for web requests).

Fields:

| Field | Type | Required | Details |
|-------|------|:--:|---------|
| **Name** | text | ✅ | "e.g. Filesystem Server". Displayed in the server list. In Add mode a small "auto" badge appears on the adjacent ID field until the user manually edits it. |
| **ID** | text | ✅ | "e.g. filesystem". Lowercase letters/digits/underscores/hyphens only, must start with a letter or digit (regex: `^[a-z0-9][a-z0-9_-]*$`). In Add mode the ID auto-derives from the Name (spaces → hyphens, lowercased) until you edit the ID manually  -  after that it stops tracking the Name. |
| **Description** | text |  -  | Free-form; shown inside the server card when expanded. |
| **Command** | text | ✅ | The executable to launch. Hint: "Full path may be needed (e.g. `C:/Program Files/nodejs/npx.cmd`)". |
| **Arguments** | dynamic list |  -  | Each arg is its own row with an X to remove. `+ Add argument` appends another row. Empty rows are filtered out on save. |
| **Environment Variables** | textarea |  -  | `KEY=VALUE`, one per line. Lines starting with `#` are ignored. |

Submit-button label: **Add & Discover Tools** (Add mode) or **Save & Rediscover** (Edit mode). Submitting triggers an immediate discovery pass  -  the server is launched and its tool manifest pulled. If discovery fails the card shows a banner `Server added but discovery failed: {error}`.

**The Server list** (`components/tools/MCPServerPanel.svelte`):

Each server renders as a collapsible card with this header row:
- **Status dot** (8 px)  -  gray for disabled, yellow if no tools were discovered, green once tools are discovered.
- **Name** (bold) and **ID** (muted, monospace).
- **Tool count badge**  -  `{N} tools`.
- **Updated-at timestamp** (relative).
- **Enable toggle**  -  flips the server on/off without deleting it.
- **Chevron**  -  expand/collapse.

**When a card is expanded** (and not in edit mode), it shows:
- The full command line rendered as `<code>{command} {args}</code>` for quick verification.
- The description (if set).
- Any recent test-result banner (auto-dismisses after ~8 s): green `Connected  -  {N} tools found` on success, red with the specific error on failure.
- **DISCOVERED TOOLS** list  -  each row shows the raw MCP tool name + description + its own enable toggle. The generated Nymeria runtime ID is available from the row tooltip and expanded server details, but it is not the primary label. Enabling a tool here adds `mcp__{server_id}__{tool_name}` to the default tool set behind the scenes. Enabled tools get a thin accent rail on the left edge. Empty state: "No tools discovered yet. Try rediscovering."
- **Server actions row**  -  four buttons:
  - **Test**  -  connects and reports tool count or error. Disabled while in flight.
  - **Rediscover**  -  re-runs discovery (useful after editing the server's source code or updating its package).
  - **Edit**  -  expands an inline `MCPServerForm` in Edit mode inside the card.
  - **Delete**  -  two-click confirm: first click reveals "Delete this server?" + Confirm (red) + Cancel.

**Empty state** (no servers, form closed): terminal icon + "No MCP servers configured" + "Add a server to auto-discover its tools".

**Typical add-an-MCP flow** the LLM should walk a user through:
1. Open Settings (gear icon in the sidebar footer) → **MCP** tab.
2. Click **Add Server** or **Install Server**.
3. If installing a standard server, click the **Filesystem** or **Fetch** preset to auto-fill the command and args. Otherwise type the command and add each arg as its own row.
4. Give it a Name; the ID fills itself. Add a description if you want.
5. Add any required environment variables (API keys, etc.), one `KEY=VALUE` per line.
6. Click **Add & Discover Tools**. The card appears; green dot = discovery succeeded.
7. Click the card to expand, then flip on the individual tool toggles you want the agent to have access to.
8. The tools are now in the agent's default tool set for all threads.

**2.6.6 Skills** (desktop global/library management)
- Lists installed skills grouped by scope: User-installed, Global, Bundled.
- Each skill row: name, description, badges for "has scripts", "has references", "has assets", "allowed tools count", a Remove button (if not bundled), and an "Enable globally" checkbox.
- **Browse Marketplace** button opens a panel listing skills in the official `anthropics/skills` repository with per-skill install buttons.
- Mobile does not expose marketplace install, uninstall, or global skill defaults. Mobile Thread Settings has a Skills tab for installed-skill visibility and per-thread enable/disable overrides.

**2.6.7 CLIProxy** (desktop only; unavailable on mobile)
- Status: Running / Stopped.
- Start/Stop button.
- Authenticated sessions list (provider, email).
- Add Login buttons (Claude / OpenAI)  -  each opens an OAuth flow in the system browser.
- **Use CLIProxy for LLM**  -  sets base URL to `http://localhost:8317`.
- **Use Direct API**  -  clears the base URL override.

**2.6.8 Voice** (surfaced inside LLM or as its own section depending on build)
- TTS provider (none / OpenAI / custom), model, voice, output format, speed (0.25–4.0).
- STT provider (none / OpenAI / custom), model, language code.
- Default thread for voice.
- Note: these settings wire up the `/voice/*` REST endpoints. The desktop chat UI itself has no mic button.

**2.6.9 Account** (always visible)

Self-management for the currently signed-in account. Sections:
- **Identity**  -  avatar, email (read-only), display name (editable, save sends `PATCH /me`), role chip, account ID.
- **API tokens**  -  table of every token issued for this account, each with a hash-prefix label, optional human label ("iPhone", "discord-bot script"), creation time, last-used time. Buttons: **Issue token** (opens a small modal asking for an optional label, then a copy-once dialog with the raw token  -  copy it now, it cannot be retrieved later). Per-row trash icon revokes that token; any client using it gets a 401 on its next request and is signed out.
- **Linked platforms** (admin only)  -  chat-platform IDs that route messages from those platform users back to this account. Add via dropdown + ID input; remove via per-row trash.
- **Sign out** (destructive button)  -  clears the local session and routes back to the Setup Wizard.

**2.6.10 Users** (admin only  -  tab is hidden for non-admins)

Admin panel for managing every account on this Nymeria install.
- **Search bar** filters by email, display name, role, or account ID.
- **+ New user** button opens a modal asking for email + optional display name + role (user/admin) + advanced options (custom user ID, initial token label). Submit creates the account and surfaces the freshly-issued raw token in the same copy-once dialog.
- **Table** lists every account: avatar, name + role chip, status badges (`Disabled`, `You` for the calling account), email, token count, "last seen" relative time.
- **Click a row** swaps the table for a detail view with sections for Profile (editable display name + role + Enable/Disable toggle), API tokens (same as the self panel above plus a **Rotate all** action that revokes every active token and mints a fresh one), Linked platforms, and Delete user.
- **Delete user** is force-disabled when looking at yourself. The backend refuses (409) if the target still owns threads or todos  -  the UI surfaces this as "Cannot delete: still owns threads/todos" and you'll need to clean those up first. The same guard applies to demoting/disabling the only enabled admin: that returns "Cannot demote/disable the only enabled admin".

**2.6.11 Other tabs hidden for non-admins**

A few admin-gated buttons elsewhere in Settings simply disappear when the calling account isn't admin:
- **Restart server** icon in the connection status bar (the tiny refresh button).
- **Install Server** / **Add manually** buttons in the MCP Servers panel  -  replaced with an "Admin only" hint chip.
- **Self-modify** and **Claude Code** tools in the Tools panel get a small "admin only" badge next to their name. The toggle still flips, but invoking them as a non-admin returns 403  -  the badge is the up-front warning.

### 2.7 First-run setup wizard

Appears on first launch or when config is missing. Four steps:

1. **Welcome**  -  checklist: network access, backend URL, and `nym_...` account token.
2. **Backend URL**  -  text input (pre-filled with `localhost:8000`, a build-time default, or the current page origin when the web UI is served by the backend and `/health` validates).
3. **Account Token**  -  password field + **Test Connection** button. After a successful test, an identity preview card appears reading "You'll be signed in as <email> [<role>]" so you can sanity-check that the token belongs to the account you intended. Next is disabled until the test succeeds.
4. **Complete**  -  "You're All Set!" screen.

**Tauri auto-config:** source-checkout desktop launches can fetch local credentials through the `get_auto_config` Tauri command, and test builds can still use `VITE_DEFAULT_API_URL` / `VITE_DEFAULT_API_KEY` environment variables. Installed beta desktop builds are client-only, so users enter the backend URL and `nym_...` account token in the wizard.

**Backend-served browser mode:** when the desktop SPA is opened from the Nymeria API origin instead of Tauri, the wizard probes same-origin `/health`. If it returns the Nymeria health JSON, the Backend URL field is filled with that origin; the account token step is still required.

### 2.8 Startup overlay (Tauri only)

Full-screen overlay while a source-checkout Tauri launch starts a local backend:
- "Starting Nymeria…" (spinner).
- "Connecting…" (waiting for backend readiness).
- "Startup Failed" with an error message when the local backend cannot start.

Installed beta desktop builds are client-only and signal readiness immediately;
connection failures are handled by the setup wizard instead.

### 2.9 Notifications

Bell icon in the sidebar footer opens the Notification Center popover (desktop) or full-screen modal (mobile). Shows:
- Unread-count badge with a glow animation.
- List of notifications with title, body, relative timestamp.
- Click a row to mark read and (if it links to a thread) jump there.
- Dismiss (X) per row. **Mark all read** button in the header when unread.
- `Escape` closes the desktop popover.
- Polling starts automatically on app mount (~30 s interval).

### 2.10 Keyboard shortcuts (desktop)

| Shortcut | Action |
|----------|--------|
| `Ctrl`/`Cmd` + `B` | Toggle sidebar |
| `Ctrl`/`Cmd` + `Shift` + `B` | Toggle dashboard (right panel) |
| `Ctrl`/`Cmd` + `Enter` | Send message |
| `Ctrl`/`Cmd` + click (thread) | Toggle thread into multi-select |
| `Shift` + click (thread) | Range-select threads |
| Double-click (thread) | Inline rename |
| Right-click (thread or folder) | Context menu |
| `Enter` (while editing title) | Save |
| `Escape` (while editing title) | Cancel |

### 2.11 Attachment constraints

- Max 10 files per message.
- Typical size limit: 25 MB per file (enforced server-side; frontend surfaces errors in a red banner below the input).
- Supported: images (jpg, png, gif, webp), documents (pdf, txt, md, csv, json, xml), source code (js, ts, py, java, cpp, c, rust, go, rb, php, cs, swift, kt, …).
- Attachments are scoped to the message; they are not stored as persistent workspace files unless a tool writes them out.

### 2.12 Persistence

- LocalStorage (prefix `nymeria-`): API URL, API key, current thread ID, theme, sidebar/dashboard collapsed state, last-notification-fetch timestamp, attachment-warning suppression, cached account identity from `/me`.
- **Per-user namespacing:** once `/me` resolves and Nymeria knows your account ID, most per-feature keys are silently re-prefixed to `nymeria-<user_id>-*` (threads, current thread, folders, sort mode, UI state). This means switching accounts shows the right person's history. The unscoped legacy keys are migrated once per user and left in place for any future identity that hasn't been seen yet.
- Browser-level cache of the currently open thread's messages; cleared on thread switch and reloaded on return.

### 2.13 Account menu and switcher

The avatar in the sidebar footer is the entry point to all identity-related actions. Click it to open the **Account Menu**:

| Item | Action |
|---|---|
| Header card | Avatar + display name + email + role chip + the server hostname you're connected to |
| **Manage account** | Opens Settings → Account tab (see §2.6.9) |
| **Manage users** (admin only) | Opens Settings → Users tab (see §2.6.10) |
| **Switch account** | Opens the Account Switcher panel  -  see below |
| **Add account** | Opens the Add Account modal  -  see below |
| **Sign out** | Clears the local session and routes back to the Setup Wizard |

**Account Switcher** lists every saved account with its real avatar + display name + role chip + server hostname. The currently active row is marked with a check icon and an accent border. Hover any row to reveal a `⋯` menu with **Re-verify** (re-fetches `/me` for that entry  -  useful if the token was just revoked or the role changed), **Rename**, **Remove**. Click an inactive row to switch  -  the chat clears, threads reload from the new backend, and a confirmation toast appears.

**Add Account** is a modal with two fields (server URL + token). Click **Test connection** to run `/health` then `/me`; on success a preview card appears showing the resolved identity ("You'll be signed in as alice@example.com [admin]") and an optional **Label** field (defaults to the resolved display name). **Save and switch** persists the entry to your saved-account list and immediately switches to it.

> **Mobile difference:** the Account Switcher and Add Account modal are **desktop-only** because the mobile app is single-connection. On mobile, the same avatar opens a full-screen Account sheet with Manage account / Manage users / Connection settings / Sign out. To use a different token on mobile, go to Settings → Connection.

### 2.14 Error toasts

Whenever an action talks to the account or admin endpoints (issuing a token, switching account, deleting a user, etc.), failures surface in a stack of toasts in the top-right corner:

| Toast title | When it fires | What to do |
|---|---|---|
| **Session expired** | Your token was revoked, expired, or the account was disabled (any 401). | Sign in again  -  the toast persists and your session is automatically cleared. |
| **Admin role required** | You tried an action gated by admin (any 403 from `/admin/*`). | Switch to an admin account, or ask one to act on your behalf. |
| **Last admin** | You tried to demote or disable the only enabled admin (409). | Promote another user to admin first. |
| **Cannot delete** | You tried to delete a user who still owns threads or todos (409). | Re-assign or delete those resources first. The user detail view shows the counts. |
| **Something went wrong** | Any other non-2xx. | Read the message  -  it's the backend's own `detail` field. |

Toasts auto-dismiss after 5 seconds (except Session expired, which is sticky).

### 2.15 Credential prompt panel

When the agent calls the `request_credential` tool (e.g. because it needs an API key for a new MCP server or to start an OAuth sign-in), the desktop renders a **non-blocking floating panel** in the top-right corner instead of a full-screen modal. File: `components/credentials/AuthPromptModal.svelte`. Global store: `stores/authPrompt.svelte.ts`. Wired into the SSE pipeline in `stores/autonomous.svelte.ts` (handlers for `auth_prompt`, `auth_prompt_resolved`, `auth_prompt_cancelled`).

**Behavior:**

- The panel does **not** block the rest of the app. There is no backdrop, no focus trap, no z-index hijack. The user can keep chatting, scroll, open Settings, switch threads, etc. while the panel is on screen.
- **Drag** the panel by its header to reposition. The panel clamps inside the viewport on each move; on window resize the position is preserved.
- **Dismiss** with the X in the header. Dismiss reports the last test error (if any) back to the server for audit, but never sends a user_message back into the thread.
- The panel is **global, not thread-scoped**. Even if the user navigates away from the thread that issued the prompt, the panel stays visible.
- On a successful submit (or OAuth callback) the panel just closes. **No agent turn fires automatically.** The user prompts the agent in chat (e.g. "ok, try again now") to continue. This is intentional, the framework no longer guesses when to wake the agent.
- On a test failure the error renders inline in the panel with a small icon-only **copy button** at the top-right of the alert. The user can paste the failure back into chat for the agent to debug.

**Mode-specific UI inside the panel:**

| Mode | What renders |
|---|---|
| `api_key` / `pat` / `form` | Form with one field per declared `fields[]` entry, optional "Name this connection" label input, Cancel / Test / Save buttons. Secret inputs use a monospace font; the connection-label input uses the regular UI font. |
| `oauth` (authorization code) | "Sign in with X" button that opens the auth URL in the OS browser via the Tauri opener, then shows a "Waiting for sign-in…" indicator. The browser callback resolves the future server-side. |
| `oauth_device` (RFC 8628) | Large monospace user code with a copy button, verification URL with an "Open" button (or "Open with code pre-filled" when the provider supplies `verification_uri_complete`), and a live countdown. Renders a shimmer skeleton if the code hasn't arrived yet. |

**Above the form** the agent can pass:

- `description` (markdown)  -  short summary of what the connection is for. Renders as a paragraph below the title.
- `instructions` (markdown)  -  step-by-step guidance tailored to context. Renders as a left-border accent callout labelled "Steps from Nymeria" between the description and the form. Use this for "go to Settings → API Keys → Create" style walkthroughs.

**Existing-accounts banner**: if the user already has connected accounts for the same provider, a small banner above the form lists them and prompts "Add another?".

**Mobile parity**: not implemented yet. The mobile app silently ignores `auth_prompt` SSE events. Credential setup on mobile currently has to go through Settings → Connections.

**Bots** (Discord, Telegram, Twitch): instead of a modal, the bot replies with a text message containing the one-time setup URL (or sign-in URL / device code for OAuth). Same prompt, different surface. If the agent passes `instructions`, they are appended below the URL/code, truncated at ~800 chars to fit message limits.

---

## 3. Mobile App

**File location:** `nymeria-mobile/src/lib/components/`. Entry route: `src/routes/+page.svelte`. Shell: `components/layout/MobileShell.svelte`. Diffs versus desktop:

### 3.1 Navigation

No collapsible panels. Three 100-vw scroll-snapped panels in a horizontal row: **Left** (Threads + Settings + Notifications) / **Center** (Chat) / **Right** (Dashboard).

- Swipe left or right to move between panels.
- Three indicator dots at the top. Active dot stretches from 6 px round to 20 px pill. Tapping a dot jumps to that panel.
- Haptic feedback (`light` impact) on panel change.
- Android hardware back button: if not on the center panel, returns to the center (chat). If already on center, exits the app.

### 3.2 Left panel

- "New Chat" button (circular, 44 × 44 touch target, icon-only).
- Thread list  -  simplified: no folders, no multi-select, no sort picker. Text search input at the top filters threads by title.
- Footer: condensed connection status + three 40 × 40 buttons  -  **Account avatar** (opens a bottom-sheet account menu with Manage account / Manage users (admin only) / Connection settings / Sign out), **Settings** icon, **Notifications** icon (with badge when unread).

### 3.3 Chat panel

- No Outlook quick-actions bar (Outlook mode is desktop-only).
- Input textarea caps at 120 px (vs 200 px on desktop).
- **Send with plain `Enter`**. `Shift+Enter` inserts a newline. (Reversed from desktop.)
- Send button is a 44 × 44 circular button with a scale animation on tap. Flips to Stop while streaming.
- Input has a Camera button in addition to the paperclip  -  tapping it opens Capacitor's Camera plugin.
- Input font-size is forced to 16 px to prevent iOS auto-zoom.
- Padding-bottom uses a CSS variable `--keyboard-height` managed by Capacitor's Keyboard plugin so the input rides above the on-screen keyboard.
- Safe-area insets respected (`env(safe-area-inset-*)`) so content does not render under notches or the home indicator.

### 3.4 Settings modal

Full-screen with a back button, title, and a tab bar at the top. Same core tabs as desktop minus **CLIProxy** and minus the global **Skills** library panel. Per-thread skill enable/disable lives in mobile Thread Settings, while the marketplace panel is desktop-only. Touch targets enlarged to 44 × 48 px minimum.

The **Account** and **Users** tabs (§2.6.9 and §2.6.10) are present on mobile too. The Users tab on mobile uses the same master/detail flow as desktop, with the detail view stacking form fields vertically. Users tab is hidden when the calling account is not admin. Mobile has no Account Switcher / Add Account modal  -  token swapping happens through the Connection tab.

### 3.5 Lifecycle

- Background: polling pauses, SSE disconnects, state mirrored to Capacitor Preferences (backup of localStorage).
- Foreground: polling resumes, SSE reconnects, state restored from Preferences if localStorage was cleared.

---

## 4. Discord Bot

**Code:** `Nymeria/nymeria/triggers/discord_bot.py`. Deep docs: `Nymeria/docs/discord-bot.md`.

### 4.1 Invocation

- **Slash commands** (`/ask`, `/help`, …)  -  available in DMs and in any guild channel where the bot is invited.
- **Mentions** (`@Nymeria your message`)  -  in guild channels the bot's response mode determines behavior.
- **Direct messages**  -  plain text messages always get a reply (no prefix or mention needed).

**Response modes** (set by the administrator via `DISCORD_RESPOND_MODE` env var):
| Mode | Behavior in guilds |
|------|-------------------|
| `mention` (default) | Bot only replies when `@`-mentioned. |
| `all` (legacy) | Bot replies to every message in every channel the bot can read. |

DMs always respond regardless of mode.

### 4.2 Full command reference

**Chat commands**

| Command | Arguments | Purpose | Notes |
|---------|-----------|---------|-------|
| `/ask` | `<message>` | Send a message to Nymeria | Streamed, edited in place ~1.5 s per update. Max 2000 chars per message; long responses split at paragraph / line / sentence boundaries, preserving code fences. |
| `/stop` |  -  | Abort the current agent run | The partial reply already posted stays. |
| `/clear` |  -  | Clear conversation history | Preserves notepad and per-channel tool config. Ephemeral reply. |
| `/compact` |  -  | Compact context window | Reports how many turns were summarized. |
| `/thread` |  -  | Show thread ID, token usage, compaction count | Ephemeral. Thread ID is `discord_{guild_id}_{channel_id}` or `discord_dm_{channel_id}`. |
| `/context` |  -  | Detailed context breakdown | Model, tokens, tools, thread-level overrides, custom instructions. |
| `/tasks` | `[status: active\|pending\|in_progress\|done\|all]` | List scheduled TODOs (short form) | Default `active`. Shows up to 10. |
| `/export` | `[format: markdown\|json\|txt]` | Export conversation as a file | 25 MB Discord upload limit. Filename `nymeria-{channel}-{YYYYMMDD}.{ext}`. |
| `/restart` | `[target: bot\|api]` | Restart bot (default) or API | Docker brings the container back automatically. |
| `/help` |  -  | List available commands | Ephemeral. |

**Model & thinking**

| Command | Arguments | Purpose |
|---------|-----------|---------|
| `/model` | `[name] [scope: global\|thread]` | Show or change the LLM model; scope picks whether the change is server-wide default or just this channel. |
| `/models` |  -  | List all available models from the current provider with context-window sizes. |
| `/think` | `[mode: off\|on\|low\|medium\|high]` | Show or set extended-thinking mode. `low`/`medium`/`high` map to reasoning-effort levels for reasoning models. |
| `/status` |  -  | Full dashboard: model, provider, context bar, tools count, uptime, watchdog, task counts, respond mode, channel-context state. |

**TODOs group** (`/todos`)

| Sub-command | Arguments | Purpose |
|-------------|-----------|---------|
| `list` | `[filter: active\|pending\|in_progress\|done\|all]` | List TODOs. Shows ID (first 8 chars), task, scheduled time, recurrence, thread. |
| `add` | `<task> [schedule] [repeat] [notes]` | Create a TODO. Schedule accepts relative strings like `45s`, `17m`, `2h`, `1w` or absolute/ISO datetimes like `2026-12-25 14:00`. Repeat options: `5min` through `monthly`. |
| `complete` | `<todo_id>` | Mark done (partial ID, first 8 chars). Recurring TODOs auto-reschedule. |
| `delete` | `<todo_id>` | Delete permanently. |

**Config group** (`/config`)

| Sub-command | Arguments | Purpose |
|-------------|-----------|---------|
| `show` |  -  | Show all backend settings grouped by category. |
| `get` | `<key>` | Get a specific setting. |
| `set` | `<key> <value>` | Update a setting. Auto-parses booleans, numbers, `none`. Some settings require `/restart api` (the reply says so if needed). |

**Environment-variable group** (`/env`)

| Sub-command | Arguments | Purpose |
|-------------|-----------|---------|
| `show` |  -  | Show all env vars; secrets masked with 🔒. |
| `get` | `<key>` | Get the unmasked value. |
| `set` | `<key> <value>` | Set an env var. Auto-type-conversion. May require API restart. |

**Tools group** (`/tools`)

| Sub-command | Arguments | Purpose |
|-------------|-----------|---------|
| `core` |  -  | List the default core tools. |
| `optional` |  -  | List optional categories with per-channel enabled counts. |
| `enabled` |  -  | Show all tools currently active in this channel (🟢 enabled, ❌ disabled core). |
| `category` | `<name>` | List tools inside a named category. |
| `enable` | `<name>` | Enable a tool or a whole category in this channel. Tab-autocomplete both. |
| `disable` | `<name>` | Disable a tool (works for core too) or a whole category. |

Tool overrides are per-channel and persist across bot restarts (they live in the backend's thread config).

**Memory group** (`/memory`)

| Sub-command | Arguments | Purpose |
|-------------|-----------|---------|
| `list` |  -  | List the user's saved memories. |
| `save` | `<key> <value>` | Save a memory (persists across conversations). |
| `forget` | `<key>` | Remove a memory. |
| `search` | `<query>` | Search memories (matches keys and values). |

**Notepad group** (`/notepad`)

| Sub-command | Arguments | Purpose |
|-------------|-----------|---------|
| `read` |  -  | Read this channel's notepad. |
| `write` | `<content> [mode: append\|replace]` | Write to the notepad. Default `append`. |
| `clear` |  -  | Clear the notepad. |

**Channel toggles**

| Command | Purpose |
|---------|---------|
| `/show-tools` | Toggle whether tool calls are rendered as embeds. Default: hidden (boundaries shown as `────` separators inside the response). When on: blue embed for tool call, green embed for result. |
| `/channel-context` | Toggle whether `/ask` and @mentions prepend the last ~10 non-bot messages in the channel as context. Default: on. |

### 4.3 Autonomous delivery

The bot maintains a persistent SSE connection to `/autonomous/stream`. When a TODO created in a given channel fires, the result is posted back as an embed in that same channel. No per-user toggle.

### 4.4 Thread model

- Guild channel: `discord_{guild_id}_{channel_id}`.
- DM: `discord_dm_{channel_id}`.
- One channel ↔ one Nymeria thread. History preserved across bot restarts. `/clear` wipes history but keeps the channel mapped to the same thread ID.
- Users cannot switch threads from inside Discord. Each channel is permanently bound to its thread.

### 4.5 Attachments

- `/ask` slash command does **not** accept attachments (Discord slash-command limitation).
- Plain @mentions and DMs **do** accept attachments.
- Images (jpeg, png, gif, webp) up to ~10 MB, documents (pdf, txt, md, csv) up to ~20 MB, max 4 files per message.
- The bot auto-sets `force_unsupported_attachments=true` so the backend does not prompt  -  Discord has no way to show the attachment-compatibility warning modal.
- Outbound attachments (when Nymeria calls `file_write(..., attach=True)`) are downloaded from `/workspace/download` and re-uploaded as Discord attachments.

### 4.6 Streaming UX

- With tool-call display off (default): the response edits in place into a single (or few) messages; each tool boundary is visually rendered as an inline `────────` separator.
- With tool-call display on: each text run posts as a message, each tool call posts as a blue embed, each tool result posts as a green embed. More text → more messages.
- `/stop` aborts  -  the partial message stays visible.
- Cooldowns: `/ask` has a 30 s per-user and 10 s global cooldown. Hitting the cooldown returns "Cooldown! Try again in Xs".

### 4.7 End-user setup

1. Create an application at https://discord.com/developers/applications. Copy the bot token from Bot → Reset Token.
2. Enable the **Message Content Intent** under Privileged Gateway Intents.
3. Under OAuth2 → URL Generator, pick scopes `bot` + `applications.commands`, permissions Send Messages, Read Message History, Use Slash Commands.
4. Visit the generated URL to invite the bot.
5. Give the bot token to your admin for their `.env.docker` as `DISCORD_BOT_TOKEN`.

After the bot restarts, slash commands sync automatically. Guild commands appear within seconds; if they do not, close and reopen Discord's slash menu.

---

## 5. Telegram Bot

**Code:** `Nymeria/nymeria/triggers/telegram_bot.py`. Deep docs: `Nymeria/docs/telegram-bot.md`.

### 5.1 Invocation

- **Private chat (DM):** every plain-text message is a prompt. No prefix needed.
- **Groups:** Telegram's bot privacy mode hides messages by default; the bot only sees explicit `/commands` and replies to its own messages.
- **Commands** use the `/command_name` form (underscored, Telegram style).

### 5.2 Full command reference

Same capability surface as Discord, with different command names (Telegram style).

**Chat**

| Command | Arguments | Purpose |
|---------|-----------|---------|
| `/start` |  -  | Telegram entry-point; shows a welcome message. |
| `/ask` | `<message>` | Send a message. Streamed, max 4096 chars per message. |
| `/stop` |  -  | Abort current run. |
| `/clear` |  -  | Clear history. |
| `/compact` |  -  | Compact context. |
| `/thread` |  -  | Show thread ID (`telegram_{chat_id}`), usage, compaction count. |
| `/context` |  -  | Detailed context breakdown. |
| `/tasks` | `[status]` | List scheduled TODOs (short form). |
| `/export` | `[format]` | Export history as a file attachment. |
| `/restart` | `[bot\|api]` | Restart bot or API. |
| `/showtools` |  -  | Toggle tool-call display. Default off (tool calls not shown; boundary implied by separate message bubbles). When on: tool calls and results appear as extra messages formatted in HTML. |
| `/help` |  -  | List commands. |
| (plain text in DM) |  -  | Same as `/ask`, no prefix required. |

**Model & thinking**

| Command | Arguments |
|---------|-----------|
| `/model` | `[name] [scope]` |
| `/models` |  -  |
| `/think` | `[mode]` |
| `/status` |  -  |

**TODOs** (pipe-delimited args)

| Command | Arguments |
|---------|-----------|
| `/todo_list` | `[status]` |
| `/todo_add` | `<task> \| <schedule> \| <repeat> \| <notes>`  -  pipe-separated. |
| `/todo_complete` | `<id>` |
| `/todo_delete` | `<id>` |

**Config**

| Command | Arguments |
|---------|-----------|
| `/config_show` |  -  |
| `/config_get` | `<key>` |
| `/config_set` | `<key> <value>` |

**Env vars**

| Command | Arguments |
|---------|-----------|
| `/env_show` |  -  |
| `/env_set` | `<key> <value>` |

**Tools**

| Command | Arguments |
|---------|-----------|
| `/tools_core` |  -  |
| `/tools_optional` |  -  |
| `/tools_enabled` |  -  |
| `/tools_category` | `<name>` |
| `/tools_enable` | `<name>` |
| `/tools_disable` | `<name>` |

**Memory**

| Command | Arguments |
|---------|-----------|
| `/memory_list` |  -  |
| `/memory_save` | `<key> <value>` |
| `/memory_forget` | `<key>` |
| `/memory_search` | `<query>` |

**Notepad**

| Command | Arguments |
|---------|-----------|
| `/notepad_read` |  -  |
| `/notepad_write` | `<content>`  -  prefix with `replace:` to overwrite instead of append. |
| `/notepad_clear` |  -  |

### 5.3 Streaming UX

- Streamed into separate message bubbles (Telegram's native layout gives natural separation even without tool-call display).
- An inline keyboard with a "Stop" button appears on the first message and lets the user abort mid-stream.
- Edits happen at ~1.5 s intervals.
- If the LLM emits malformed HTML, the bot automatically falls back to sending the response as plain text.

### 5.4 Attachments

- Photos (sent as photo or document) and documents (pdf, txt, md, csv).
- Up to 10 MB for photos, 20 MB for documents, max 4 per message.
- The message's `caption` is used as the prompt text; if absent, the text `[attachment]` is substituted.
- Outbound attachments: images under 10 MB send as inline photos, other files under 50 MB (Telegram's cap) send as documents.

### 5.5 Thread model

One chat (private or group) ↔ one Nymeria thread: `telegram_{chat_id}`. Persisted across restarts. Users cannot switch threads from within Telegram.

### 5.6 End-user setup

1. Message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts.
3. Copy the bot token (format `123456789:ABCdef…`).
4. (Optional) Set description/picture with BotFather.
5. Provide the token to your admin as `TELEGRAM_BOT_TOKEN`.

Autonomous TODO deliveries flow into the originating chat as separate messages. No per-user toggle.

---

## 6. Twitch ⚠️ Removed / Disabled

⚠️ **The standalone Twitch bot has been removed**, and the Twitch agent tools are **currently disabled** pending a rewrite to standalone optional tools (their own OAuth plus direct Twitch Helix calls, no running bot). There is no Twitch chat bot, no `!` commands, and no pulse loop today.

**What to tell users who ask about Twitch:**

> Twitch support is being reworked. The old Twitch chat bot has been removed and the Twitch tools are temporarily disabled while they are rebuilt as standalone tools. There is no Twitch functionality available right now; this document will be updated when the rebuilt tools ship.

---

## 7. Outlook Add-in

**Code:** `nymeria-desktop/outlook-addin/manifest.xml`, `nymeria-desktop/src/lib/stores/outlook.svelte.ts`, `nymeria-desktop/src/lib/components/outlook/QuickActions.svelte`. Deep docs: `Nymeria/docs/outlook-addin.md`.

> Note on scope: only the **Ref Email** button documented below is part of the consumer Outlook surface. Do not describe any other Outlook quick-action buttons as features a general user has.

### 7.1 What the add-in is

A sidebar ("taskpane") that Outlook loads inside itself when the user clicks the Nymeria button in the ribbon while viewing an email. It renders the full desktop app UI with two special behaviors:

1. It reads the currently-viewed email's subject, sender, and received date through Office.js and keeps that context live as the user switches emails.
2. It runs in single-panel mode  -  because the taskpane is narrow, opening the right dashboard automatically closes the sidebar, and vice versa.

### 7.2 Installation (end user)

Three paths:

1. **Outlook on the web (easiest):** outlook.office365.com → Settings → Integrated apps → Upload custom app → select `manifest.xml`. Syncs to the desktop client automatically.
2. **Microsoft 365 admin deployment:** admin center → Settings → Integrated apps → Upload custom app → assign to users.
3. ⚠️ **New Outlook desktop (limitation):** no "Add from file" option. Sideload via the web first; it will appear in the desktop client once synced.

Manifest identity: App ID `<your-app-id>`, version `1.0.0.0`, taskpane URL `https://<your-taskpane-host>/?outlook=1`, fixed height 450 px, permission `ReadItem` (read-only to mailbox).

### 7.3 Ref Email button

The single consumer-facing Outlook-specific UI. It appears in the Quick Actions bar above the chat (visible only when in Outlook mode and an email is selected).

- **What it does:** inserts the current email's subject, sender, received date, and Outlook item ID into the chat message composer as a structured context block.
- **Why:** the agent can then use its Outlook tools (`outlook_search_emails`, `outlook_get_email`) to locate and operate on that exact email  -  draft a reply, summarize the thread, extract attachments, etc.
- **Typical workflow:**
  1. Open an email.
  2. Click **Ref Email** (the `@` button in the Quick Actions bar).
  3. Type your request in the composer ("summarize this thread" / "draft a polite decline" / "save attachments to the downloads folder").
  4. Send. The agent picks up the email via the referenced ID and acts on it.

### 7.4 Email-related tools the agent has access to

These tools are backend-registered and available to the agent when the user's `outlook_*` tools are enabled. The user does not call them directly; they send natural-language prompts and the agent picks tools.

| Tool | Purpose |
|------|---------|
| `request_credential(provider="outlook", kind="oauth")` + `auth_inspect`/`auth_cleanup` | OAuth connect (auth-code or device-code), connection listing, and disconnect  -  all via the unified credential vault |
| `outlook_list_emails` | List emails in a folder with filters |
| `outlook_get_email` | Fetch one email's full content |
| `outlook_search_emails` | Keyword / filter search |
| `outlook_send_email` | Send a new email |
| `outlook_reply_email` | Send a reply |
| `outlook_forward_email` | Forward |
| `outlook_create_draft` / `outlook_draft_reply` / `outlook_edit_draft` | Draft management |
| `outlook_delete_email` | Delete |
| `outlook_mark_email` | Read/unread, flag/unflag |
| `outlook_move_email` | Move to folder |
| `outlook_set_category` | Apply Outlook categories |
| `outlook_get_attachments` | Extract text/binary attachments |

### 7.5 Office.js quirks that affect the user experience

- `confirm()` dialogs silently return `false` inside Outlook's webview. The add-in uses custom modals instead; if a user sees a confirmation prompt that appears to ignore their choice, report the specific screen.
- Taskpane width is ~350 px on Outlook Web; desktop Outlook's taskpane is resizable. Some UI (multi-column dashboards) is intentionally collapsed in this narrow mode.
- Pop-out, browser history search, and other browser-native features are unavailable inside the taskpane.
- If Outlook closes the taskpane's webview while a response is streaming, the SSE connection can drop. The frontend polls `/threads/{id}/history` every ~3 seconds while an autonomous run is in progress as a safety net.
- The `Office.context.mailbox.item.itemId` (EWS format) is **not** the same identifier Microsoft Graph uses. The add-in captures subject, sender, and date alongside the EWS ID so the agent can locate the email robustly via `outlook_search_emails`.

---

## 8. Interactive CLI

**Launch:** `nymeria cli [options]` when the console script is installed, or `python3 run.py cli [options]` from a source checkout. Code: `Nymeria/nymeria/triggers/cli/`.

The CLI is now a terminal client with a transport layer, normalized stream events, reducer-owned state, renderer-specific output, and a slash-command registry. It can run as a full-screen `prompt_toolkit` app, a Rich compatibility REPL, or a plain renderer for pipes and logs.

Maintainer research exists for a future OpenTUI-based terminal app, but it is
not shipped and is not part of `nymeria cli`. If built later, that app will be a
standalone API client with its own runtime and binary, not a fourth
`--renderer` value for this CLI.

### 8.1 Launch flags and startup

| Flag | Values / default | Purpose |
|------|------------------|---------|
| positional thread ref | unset | **(Deprecated  -  use `-r`/`--resume`.)** Open an existing thread by ID or title. Still works but emits a warning. |
| positional `list` | unset | List threads and exit. |
| `--thread`, `--thread-id`, `-t` | unset | **(Deprecated  -  use `-r`/`--resume`.)** Explicit equivalent of the positional thread ref. |
| `--user-id` | `default` | User/account ID used for API act-as routing and local state. |
| `--transport` | `api`, `auto`, `local` / `api` | Select REST/SSE API transport or the explicit in-process local agent transport. |
| `--renderer` | `auto`, `full`, `rich`, `plain` | Select full-screen TUI, Rich REPL, or plain output. |
| `--api-url` | unset | API URL for API transport. Precedence: flag, `NYMERIA_API_URL`, saved CLI profile, then `http://localhost:8000`. |
| `--api-key` | unset | API token. Precedence: flag, `NYMERIA_CLI_API_KEY`, `NYMERIA_SERVICE_TOKEN`, `NYMERIA_API_KEY`, then saved CLI profile. |
| `--no-alt-screen` | off | Keep the full-screen renderer out of the terminal alternate screen. |
| `--no-animation` | off | Disable spinner/status animation. |
| `--ascii` | off | Prefer ASCII spinner/border-safe output. |
| `--rich-scroll-region` | on | Default Rich REPL mode that uses an inline follow-footer until the viewport fills, then pins the footer below a transcript scroll region. `NYMERIA_CLI_RICH_SCROLL_REGION=0` disables it by default. |
| `--no-rich-scroll-region` | off | Disable the Rich scroll-region follow-footer path and use the older prompt_toolkit-safe Rich REPL output path. |
| `--continue`, `-c` | off | Resume the most recently updated thread. Combines with `-m` for oneshot mode against the last thread. |
| `--resume`, `-r` | unset | Resume a specific thread by ID prefix or title substring. On ambiguity, prints candidates and exits 1. Combines with `-m`. |
| `--message`, `-m` | unset | Non-interactive oneshot mode: send the given message, stream the response to stdout, and exit 0/1. Use `-` to read from stdin (e.g. `echo "summarize" \| nymeria cli -m -`). |
| `--format` | `plain`, `json`, `md`, `jsonl` / `plain` | Output format for oneshot mode (`plain`/`json`) or `--export` (`json`/`md`/`jsonl`). |
| `--export` | unset | Export a thread to a file and exit. Takes a thread ID. Use with `--format` and `--output`. |
| `--output`, `-o` | unset | Output file path for `--export` (default: auto-generated `nymeria-export-{id}-{timestamp}.{ext}`). |
| `--color` | `auto`, `always`, `never` | Color policy. `NO_COLOR` disables color unless forced. |

`nymeria cli` is thin-client first. It loads the active profile from `~/.nymeria/cli.json`, validates `/health` and `/me`, and connects to the API when credentials are valid. With no valid profile, flags, or environment token, the CLI starts disconnected and shows `Not connected. Run /login.` Chat and API-backed commands stay unavailable until login. Use `--transport local` only when you intentionally want the embedded in-process agent; the default path does not create a second backend/agent instance.

By default, `nymeria cli` starts a fresh empty thread. To resume an existing thread, use `--resume`/`-r <ref>` with an ID prefix or title substring: `nymeria cli -r "Project Review"` or `nymeria cli -r abc123`. Use `--continue`/`-c` to resume the most recently updated thread. Both flags combine with `--message`/`-m` for non-interactive use (e.g. `nymeria cli -c -m "continue where we left off"`). If the reference is missing or ambiguous, the CLI prints compact candidate matches when available and exits instead of silently creating a new thread. On Rich REPL exit, Nymeria prints `Use nymeria cli --resume <thread-id> to return to this thread.` immediately before returning to the shell prompt. The positional thread ref (`nymeria cli "title"`) and `--thread`/`-t` flag still work but emit a deprecation warning  -  use `-r` instead. `nymeria cli list` prints backend thread teams first, then ungrouped pinned threads, then ungrouped recent threads, and exits.

`/login [api-url] [--user-id <id>]` prompts for the backend URL when omitted, prompts for the API token with hidden input, validates the connection, saves the active CLI profile, and swaps the current session to API transport. `/connect` is an alias. `/logout` removes the saved active token and returns the session to disconnected mode. CLI credentials live only in `~/.nymeria/cli.json`; the parent directory is written as `0700` and the file as `0600`. Login does not write tokens into backend `.env` or `config.env` files.

**Shell completions:** `python3 run.py completion bash|zsh|fish` prints a completion script to stdout. Install with `python3 run.py completion bash > /etc/bash_completion.d/nymeria` (system-wide) or append to `~/.bashrc`. For zsh, write to a directory in `$fpath` (e.g. `~/.zsh/completions/_nymeria`). For fish, write to `~/.config/fish/completions/nymeria.fish`. Completions cover all subcommands, flags, and known choice values, generated by introspecting the argparse parser.

For Docker or remote backends, run the CLI on the host or remote terminal rather than inside the API container. Install the `nymeria` console script in that shell, run `nymeria cli`, then connect with `/login http://<backend-host>:8000`.

**Oneshot mode** (`--message`/`-m`) skips the REPL entirely. The CLI selects the transport, sends the message, streams events through an `OneshotRenderer`, and exits 0 on success or 1 on error. In plain format, only response text goes to stdout; tool calls, thinking, and errors go to stderr. In JSON format, each significant event is a newline-delimited JSON object with a `type` field (`response`, `thinking`, `tool_call`, `tool_result`, `error`, `done`). Combine with `--continue`/`-c` or `--resume`/`-r` to target a specific thread. Stdin pipe: `echo "..." | nymeria cli -m -`.

`nymeria cli` defaults to the prompt_toolkit/Rich REPL with the native scroll-region follow-footer path enabled. `--no-rich-scroll-region` opts back into the older Rich REPL output path. `--renderer full` opts into the full-screen renderer, and `--renderer auto` chooses the full-screen renderer in an interactive TTY. Both rich and auto choose plain output when stdin/stdout are not TTYs, `TERM=dumb`, or `CI` is set. `--renderer plain` sends visible response text to stdout and progress/tool/error summaries to stderr without ANSI escapes.

At Rich REPL startup, Nymeria prints a square, neutral framed header with a centered `[ Nymeria ]` title, white section labels, grey values, and subtle internal rules between dashboard groups. Because the Rich REPL is scrollback-native, the printed header caps itself at 79 columns instead of expanding to very wide terminal panes; this keeps the initial block stable under normal width changes, aligns the center divider with the centered title, and still fits inside an 80-column default terminal. Very narrow windows may still clip or reflow old scrollback. The header lays out the selected thread title/short ID/platform, pinned and callable-thread status, concise provider plus API format such as `cliproxy OAuth (messages/v1)` or `OpenRouter (responses/v1)`, model plus thinking mode such as `claude-opus-4-6 (Adaptive High)`, context usage and compaction count, counts for non-MCP tools, MCP tools, callable tools, active skills, active skill kits, and an Autonomous section listing current-thread active TODO labels and enabled trigger labels such as `Todos: Review PO, Email, +1 more` and `Triggers: Morning brief`. It also shows active config flags such as custom instructions, system prompt override, TODO/profile injection, callable name, and callable team, plus the full backend URL, API health (`api ok <ms>` or `api error`), and CLI user. Header data is best-effort: if an optional API call fails, the CLI still opens and shows the data it could fetch.

### 8.2 Full-screen TUI

The full-screen renderer has a transcript viewport, a high-contrast one-line status bar immediately above the framed bottom composer, and a labeled message input. The status bar uses a subtle theme-controlled background, brighter foreground, distinct spinner/accent color, and shows the connection mode, activity phase and duration, active model when known, thread label/ID, context usage when available, queued message count, current working directory, and short TTL-bound notices. Rich REPL status uses compact connection text such as `api ok 24ms`, `api error`, `local`, or `disconnected`; the full backend URL lives in the startup/header frame rather than the live status line.

Transcript turns are separated by width-aware labeled rules such as `──── You ────`, `──── Nymeria ────`, and `──── System ────`, with ASCII `----` fallbacks when Unicode is unavailable. During an active full-screen turn, the assistant separator mirrors the status-bar activity segment with the spinner, phase, elapsed time, and waiting tool detail, for example `──── Nymeria · ⠋ Thinking... 1.4s ────` or `──── Nymeria · ⠹ Waiting... 3.0s search_memory ────`; ASCII-only terminals use the same text with `Nymeria - ...`. Once response text starts streaming, the assistant separator drops the activity segment so the visible text becomes the live activity. Assistant turns render thinking with a readable cyan-tinted italic style and a small left rail, without a `Thinking:`/`Thought:` prefix; standard-mode one-line thinking previews end with `...` when hidden content may continue. Pre-tool response text uses the same foreground as final output, tool calls render as compact one-line rows with status/duration/previews, and final response text uses terminal-oriented Markdown. Subtle muted dividers appear after completed tool groups and at completed assistant turn ends, so each reasoning/commentary/tool pass stays visually together and the final divider marks that input is available again. Autonomous TODO/trigger output is labeled separately as `Nymeria · autonomous · <source>` where the terminal supports Unicode, or `Nymeria - autonomous - <source>` in ASCII mode.

Tool rows use status markers instead of the command-looking `>` prefix: `○` while pending/running, `✓` on success, `×` on error, and `!` when cancelled. ASCII-only terminals use `-`, `x`, and `!` fallbacks.

**Activity labels:** `Processing...`, `Thinking...`, `Formulating...`, `Compacting...`, `Waiting...`, `Processing results...`. Full-screen mode shows the live activity segment in both the active `Nymeria` transcript separator and the status bar before response text starts streaming. Rich REPL uses the same status formatter in the live line above the prompt while the prompt is active. The CLI does not put raw thinking text in the status bar. Quiet processing/thinking transitions become `Formulating...` after about one second. While the backend is compacting context, the indicator shows `Compacting...` (with the compaction message as detail). Response typing hides the transcript activity indicator.

**Autonomous output:** in API transport mode, the full-screen TUI and Rich REPL keep a background `/autonomous/stream` subscription open. TODOs, triggers, and other autonomous runs for the currently selected thread are appended to the transcript live, with `task_completed` content used as a fallback if response chunks were missed.

**Composer behavior:**
- The input is framed as `Message` and uses prompt labels such as `You:`, `Busy:`, `Queued N:`, or `Error:` instead of a bare shell-style `>`.
- `Enter` submits.
- `Ctrl+J` inserts a newline. `Esc` then `Enter` also inserts a newline in terminals that report modified Enter that way.
- A trailing unescaped backslash before `Enter` is a portable multiline fallback.
- `Tab` completes slash commands and non-leading `@file` attachment paths.
- In the Rich REPL slash-command panel, typing `/` as the first character opens the command list. `Up` and `Down` move the highlighted command, `Tab` fills the highlighted command into the composer without submitting it, and `Enter` submits the highlighted command. Exact slash-command paths show a muted inline usage hint, such as `/model [name] [global|thread]`.
- `Ctrl+R` starts history search.
- `Ctrl+X` then `Ctrl+E` opens the external editor configured for prompt_toolkit.
- `Ctrl+C` stops the active turn when busy; when idle it clears the composer.
- `Ctrl+D` exits. `Ctrl+L` redraws.

**Attachments:** in the full-screen composer and Rich REPL prompt, include `@path/to/file` after other prompt text. The CLI reads the file, sends it as an attachment, and removes the `@file` token from the message text. A leading `@...` is reserved for cross-thread dispatch and is sent to the backend unchanged.

**Cross-thread dispatch:** a leading `@ThreadName`, `@CallableName`, or quoted
`@"Thread With Spaces"` routes the prompt to that thread. The CLI keeps the
current transcript view but renders a `Response from <thread>` reference line
inside the active `Nymeria` assistant turn. Thinking previews, preamble text,
tool rows/results, workspace artifacts, and final response text are rendered by
the same pipeline used for ordinary turns.

If a user submits while a turn is active, the full-screen shell queues the message and sends it after the current turn finishes. Explicit stop/cancel preserves the current composer contents where practical.

### 8.3 Rich REPL and plain output

The Rich REPL keeps persistent history in `{data_dir}/cli_history`, command and non-leading `@file` completion, auto-suggest, Markdown response rendering, and scrollback-native output. Internally it uses a non-full-screen `prompt_toolkit.Application` so transcript output is printed safely above the live input area. Its prompt is a chat-style `You:` input rather than the old `nymeria [thread-title] > ` shell prompt, with a high-contrast status line immediately above the input showing the same compact context as full-screen mode: readiness/activity, compact connection health, model, thread, context usage, notices, and cwd. The live footer uses a flexible spacer above a thin transcript gap, prompt_toolkit resize-safe conditional toolbar-style status row, and multiline composer, so status/input sit at the terminal bottom while streamed transcript output scrolls above them. After prompt_toolkit has measured the footer position once, the footer gate stays visible across streamed redraws to avoid status-bar blinking. By default, supported POSIX/xterm-like terminals use a hybrid footer path: the status/input area starts directly under the startup header or loaded history, suppresses prompt_toolkit's automatic startup bottom-anchoring probe, is erased before transcript writes, then repainted after the newly printed transcript so it follows output downward without a startup gap. When the footer reaches the physical bottom of the terminal, Nymeria pins it below a DEC scroll region and streams transcript writes through that region instead of erasing/repainting the footer for every line; prompt_toolkit still repaints the footer on its normal refresh interval for spinner/status animation. The pinned path hides the terminal hardware cursor while moving to footer repaint anchors and while raw transcript writes are in progress, remembers prompt_toolkit's real composer cursor after footer renders, and restores that cursor after each pinned render/write so the typing cursor remains visible without repainting the footer for transcript chunks. This path does not use the removed response-event batching/splitting shim; it streams ordinary prose through the renderer's line/punctuation path while routing Markdown-looking headings, fences, tables, lists, blockquotes, rules, links, and inline-formatted lines through the stable Rich Markdown block renderer. Unsupported terminals and `--no-rich-scroll-region` fall back to the standard Rich REPL behavior. The status row refits from the live application width, and terminal resizes update the Rich renderer width before clearing and replaying the header plus visible transcript so stored turns reflow to the new width. When a launch thread ref selects an existing thread, the Rich REPL loads and prints that thread's conversation history before the first prompt. `/thread switch ...` clears the visible transcript, refreshes the header, loads the selected thread history, and keeps the live status/composer under the refreshed transcript. The live status/input area stays visible while turns run and is erased from scrollback when submitted, so old status bars do not remain between transcript turns; additional submissions are queued while the active turn streams. Live rich-mode turns mirror the full-screen transcript rhythm: labeled `You`/`Nymeria` separators, a subtle opening divider under each assistant header, stable-block streamed response Markdown, themed Markdown headings/blockquotes/lists/inline styles, GitHub-style pipe tables rendered by Rich Markdown, one standard-mode railed thinking preview for each ordered thinking step in an agent loop, compact tool rows, autonomous headers, subtle post-tool and turn-end dividers, and `/verbose on|off|status` for expanded thinking/tool transcript detail. The default theme uses softer pink/blue accents, white Markdown headings and table headers, muted separators, minimal blue for links/files/artifacts, and restrained red for errors; local `/theme` overrides still take precedence. The Rich Markdown adapter supplies Nymeria-specific render elements for left-aligned headings, calmer table edges, dim horizontal rules, and softer `nord` syntax blocks without forking Rich or `markdown-it-py`. The active response buffer is only committed at stable Markdown boundaries such as blank-line paragraph ends, closed fences, completed tables/lists/quotes, and tool/turn boundaries, so live output and resize/Ctrl-L replay use the same Rich Markdown adapter. Rich REPL block printing collapses explicit Markdown separator blanks to one visible gap and adds the same single gap after dense tables, code blocks, lists, blockquotes, and horizontal rules when tight streamed Markdown would otherwise run into the next label. Consecutive headings stay compact. Standard thinking previews are emitted after enough accumulated text fills the available preview width, or when the thinking step closes before a tool/response, so early one-word chunks do not get locked into scrollback. It does not use the alternate screen, so normal terminal scrollback and copy behavior remain available.

In the scroll-region transition specifically, the follow-mode footer is erased once before rows are reserved; this is intentionally outside the per-event pinned transcript write path so streaming does not repaint the footer on each line. While pinned, composer wrapping resizes the reserved footer rows and forces one footer repaint, but streamed transcript writes continue through the adjusted scroll region rather than repainting the footer per chunk.

In scroll-region mode, Nymeria owns prompt_toolkit terminal resize handling. Resize events reset any DEC scroll margins, clear the visible viewport and terminal scrollback, replay the header and current thread's stored Rich transcript at the new geometry, and then invalidate prompt_toolkit once so a single footer is repainted; repeated resize events are coalesced briefly so drag-resizing does not leave duplicate footer frames or replayed transcripts in scrollback. Manual `/redraw`/`Ctrl+L` remains a visible-screen recovery and does not clear scrollback.

The Rich REPL header refreshes after `/clear`, thread switch/new/rename/pin changes, login/logout or account switches, model changes, tool/skill/MCP/thread-config/trigger changes, TODO changes, compaction, and theme updates. Health is measured during startup and header refreshes only; it is not continuously polled for the status bar.

Plain mode is intended for scripts, pipes, dumb terminals, and CI logs. It never writes ANSI escape sequences. Assistant response deltas go to stdout; status lines such as `Thinking...`, tool summaries, artifacts, diagnostics, and errors go to stderr.

### 8.4 Commands

All commands begin with `/`. `/help [query]` shows grouped registry help, filters by command, description, or category, and omits duplicate default-subcommand rows such as `/thread list` appearing twice. The CLI registry merges local interactive commands with backend global slash commands through the backend command provider; duplicated global paths such as `/memory save`, `/tools core`, `/context`, and `/model <name> thread` execute through the backend command service. Aliases include `/h`, `/quit`, `/q`, `/threads`, `/t`, `/skill`, `/acct`, and the subcommand aliases listed below.

**System and diagnostics:** `/help`, `/clear`, `/exit`, `/history [--internal] [limit]`, `/settings view`, `/settings patch <key=value> [key=value...] [--yes]`, `/redraw`, `/verbose on|off|status`, `/theme show`, `/theme preset default`, `/theme set <slot> <#RRGGBB>`, `/theme reset [slot]`, `/doctor terminal`, `/doctor api`, `/doctor auth`, `/doctor model`, `/details [tool|thinking|artifact|error] [id-or-index]`.

`/verbose` affects the full-screen and Rich transcript renderers. Default `off` keeps thinking to a single railed preview row and tool calls compact; `on` expands full railed thinking text plus bounded tool argument/result details inline. Full uncapped tool payloads remain available through `/details ... --full`. The command changes the display setting only; it does not change the underlying thread or conversation state.

`/theme` changes only local CLI presentation and stores overrides in `~/.nymeria/cli.json` under the `theme` key. Colors must be `#RRGGBB`. Supported slots are `status_fg`, `status_bg`, `status_accent`, `spinner`, `prompt`, `prompt_busy`, `prompt_error`, `user_header`, `user_text`, `assistant_header`, `separator`, `thinking`, `tool`, `error`, `artifact`, and `diagnostic`. Only changed slots are persisted, so future default-theme improvements still apply to untouched slots.

**Threads, model, context:** `/thread list`, `/thread switch <id-or-title>` (`s`), `/thread new [title]` (`n`), `/thread delete <id-or-title> [--yes]` (`del`, `rm`), `/thread info`, `/thread rename <title>`, `/thread pin [id-or-title] [on|off|toggle]`, `/thread config`, `/thread compact [--yes]`, `/thread stop`, `/model [name] [global|thread]`, `/model show`, `/model set <model-id>`, `/model available [provider]` (`list`), `/models`, `/think [off|on|low|medium|high]`, `/provider`, `/provider list`, `/provider set <provider> api_key=<key>`, `/provider test [provider]`, `/provider switch <provider>`, `/fallback`, `/fallback list`, `/fallback add <model-id> [--position N]`, `/fallback remove <model-id>`, `/fallback set <model1> <model2> ...`, `/fallback clear`, `/fast`, `/fast on`, `/fast off`, `/fast set <model-id>`, `/fast <prompt>`, `/context`, `/status`, `/usage`, `/usage session`, `/compact [--yes]`. Thread references resolve as exact ID, unique ID prefix, exact title, then unique case-insensitive title substring. `/thread list` shows backend callable teams first, followed by ungrouped pinned and recent threads. `/fast <prompt>` uses the fast model for only that turn; the other `/fast` forms change the thread's model override until toggled back. `/provider` secrets live in `~/.nymeria/credentials.json` with private permissions and are applied to the backend settings API when the CLI is connected as an admin. `/fallback` manages the backend-owned `LLM_FALLBACK_MODELS` chain, so configured fallbacks apply to desktop, mobile, CLI, bots, triggers, and scheduled runs.

**Tools, skills, MCP:** `/tools list`, `/tools enable <tool-id>` (`on`), `/tools disable <tool-id>` (`off`), `/tools optional`, `/tools core` (`default`), `/tools defaults [list|add|remove|set|reset]`, `/tools test <custom-tool-id> [json-or-key=value...]`, `/skills list [scope]`, `/skills search [query] [--source source]`, `/skills install <name> [--source source] [--scope user|global]`, `/skills enable [--global] <name>`, `/skills disable [--global] <name>`, `/skills inspect <name>` (`show`), `/mcp list`, `/mcp add <source> [--name name] [--thread id] [--yes]` (`install`), `/mcp remove <server-id> [--yes]` (`rm`, `delete`), `/mcp discover <server-id>`, `/mcp test <server-id>`, `/mcp retry <server-id> [--yes]`, `/mcp status [server-id]`, `/mcp logs <server-id> [limit]`.

**Session:** `/export [json|md|jsonl] [--output PATH] [--sanitize]`, `/import <file>`.

**Personal and automation:** `/login [api-url] [--user-id <id>]`, `/connect [api-url] [--user-id <id>]`, `/logout`, `/tasks [active|pending|in_progress|done|all]`, `/todos [all|pending|in_progress|done]`, `/todos list [active|pending|in_progress|done|all]`, `/todos add <task> [| <schedule>] [| <repeat>] [| <notes>]`, `/todos complete <id>`, `/todos delete <id>`, `/todo list [all|pending|in_progress|done]`, `/todo add <task> [--schedule <when>] [--notes <text>] [--recurrence daily|weekly|monthly|hourly] [--thread current|<id>]`, `/todo edit <id> [new task] [options]`, `/todo done <id>` (`complete`), `/todo delete <id> [--yes]` (`remove`, `rm`), `/todo schedule <id> <when|clear>`, `/todo recurrence <id> hourly|daily|weekly|monthly|clear` (`repeat`), `/memory list` (`profile`), `/memory search <query>` (`find`), `/memory save <key> <value>` (`add`), `/memory forget <key>` (`delete`, `remove`), `/notepad read`, `/notepad write <content>`, `/notepad clear`, `/account current` (`me`), `/account switch <user-id>` (`su`), `/account tokens [list|issue|revoke]`, `/account platforms` (`links`), `/triggers list [--enabled] [--thread current|<id>]`, `/triggers create <name> [--source webhook] [--prompt <template>]`, `/triggers edit <id> key=value [key=value...]`, `/triggers enable <id>`, `/triggers disable <id>`, `/triggers history [trigger-id] [limit|--limit N|--limit=N]`, `/triggers test <id>`, `/triggers delete <id> [--yes]` (`remove`, `rm`), `/activity recent [limit]`, `/activity notifications`, `/artifacts recent [limit]`, `/artifacts open <index-or-path>`, `/artifacts download <index-or-path>` (`get`).

Commands prefer API/client methods. Disconnected mode reports `Not connected. Run /login.` for chat and shows unsupported-transport errors for API-backed commands. Local mode supports chat, stop, history, thread operations, backend-visible thread teams, context stats, model/thread configuration, TODO list/mutation, trigger listing, memories, and basic tool overrides; API-only surfaces such as token management, linked platforms, activity, artifacts download, skills marketplace, MCP server management, and trigger mutation/reporting commands report a clear unsupported-transport error when the local client has no matching method.

### 8.5 Streaming and errors

Both API SSE events and local `NymeriaAgent.astream()` chunks are normalized before rendering. The reducer tracks user messages, assistant response steps, thinking steps, tool calls/results, workspace artifacts, compaction notices, context stats, iteration-limit notices, queued state, errors, and completion metadata.

Full-screen mode updates the transcript and status bar from this state. Rich REPL buffers visible response text as Markdown and prints compact tool rows. Plain mode writes visible text to stdout and bounded progress/tool/error lines to stderr.

Stop/cancel is idempotent: the first stop request calls the selected transport's `stop()` endpoint or local `abort_with_cascade()`, and repeated requests report that stop is already in progress. API disconnects are treated differently from an explicit stop; disconnecting the CLI does not guarantee backend generation has stopped unless `/thread stop`, `Ctrl+C`, or the composer stop path calls the stop endpoint.

---

## 9. Cross-Cutting Concepts

These concepts behave the same everywhere  -  desktop, mobile, bots, CLI, API  -  so the UI documents above reference them without re-explaining.

### 9.1 Threads

One thread = one conversation. Everything is scoped to a thread: message history, context window, per-thread LLM override, enabled tools, enabled skills, TODOs, notepad.

**Thread ID prefixes and what they mean:**

| Prefix | Origin |
|--------|--------|
| (plain UUID) | Desktop / mobile / API default |
| `discord_{guild_id}_{channel_id}` | Discord guild channel |
| `discord_dm_{channel_id}` | Discord DM |
| `telegram_{chat_id}` | Telegram chat (private or group) |
| `twitch_{channel}` | Twitch channel |
| `slack_…` | Slack (if integrated) |
| `trigger-…` | Trigger-created thread |
| `agent-…` | Callable thread (usable as a tool by other agents) |

**Callable threads** are ordinary threads that have been given a tool name and description in their config. Other threads can then invoke them as if they were a tool. Useful for building persistent sub-agents with specialized prompts.

### 9.2 Context window & compaction

Every thread has a rolling context window sized by the active model's max context tokens. When the window fills, the backend compacts  -  it summarizes older turns and replaces them with the summary.

**Two context-management modes:**
- `auto_compact` (default)  -  summarize when the window crosses the compact threshold (default 80 %, configurable from 5-95 %).
- `sliding_window`  -  drop the oldest `N` turns rather than summarizing.

**Manual compaction** is available on every client:
- Frontend: the `Context Status Bar` above the input or the dedicated compact action in the thread menu.
- CLI: `/compact`.
- Discord: `/compact`.
- Telegram: `/compact`.
- API: `POST /threads/{thread_id}/compact`, or send `/compact` as the chat message body.

Compactions are tracked; `/context` and the Context Status Bar show how many have happened and when the last one ran.

### 9.3 Tools

Three categories:

| Category | What it is | Where it lives |
|----------|-----------|----------------|
| **Core** | Always-available, high-trust tools (file ops, bash, search, HTTP, etc.) | `ALL_TOOLS` registry in `tools/__init__.py` |
| **Optional** | Off by default; opt-in per thread or as part of the global defaults | `OPTIONAL_TOOLS` registry |
| **Callable thread** | Another thread, registered as a named tool | Per-thread config (`callable_name`) |
| **Custom HTTP / MCP** | User-defined  -  HTTP endpoints or MCP servers | Created via Settings → Tools or `POST /tools/custom` |

A thread's **effective tool set** = (global defaults ∪ per-thread enabled) − per-thread disabled.

Over ~25 enabled tools the frontend warns about performance. Performance degrades because tool descriptions are part of every turn's prompt.

### 9.4 Skills

Skills are bundles of procedural knowledge stored as directories with a `SKILL.md` plus optional scripts, references, and assets. In a thread with skills enabled the agent sees the skill's `(name, description)` pair; the full body loads only when the agent calls the `Skill(name)` meta-tool.

Scopes (precedence from highest to lowest): **project** → **user** → **bundled**. Install from the marketplace panel (desktop Settings → Skills) or drop SKILL.md directories into the scoped skills folder. Per-thread enable/disable lives in the Thread Settings → Skills tab.

### 9.5 Memories

Per-user, persistent, key-value. Accessible from any thread. Save / list / forget / search are available everywhere  -  desktop (via agent prompting), CLI (`/memory …`), Discord (`/memory …`), Telegram (`/memory_…`), and directly via `/users/{user_id}/memories*` REST endpoints.

### 9.6 TODOs (frontend label: "Tasks")

A TODO is a scheduled prompt. It has a task description, a status, optional schedule (relative string or ISO datetime), optional recurrence (`5min`, `hourly`, `daily`, `weekly`, `monthly`), and a parent thread. When it fires, the backend runs the prompt in that thread as an autonomous run; the result is streamed to whichever client originated the TODO.

**Create from:** desktop/mobile Dashboard → Tasks → Add, CLI `/todo add`, Discord `/todos add`, Telegram `/todo_add`, or API `POST /todos`.

When TODOs are changed by the agent during a streamed chat or autonomous run,
the frontend treats the resulting TODO tool `tool_result` as a dashboard
invalidation signal and refetches the current task feed without requiring a
manual refresh.

**Recurrence behavior:** completing a recurring TODO automatically reschedules it for the next interval.

**Watchdog:** if a TODO is stuck `in_progress` past the staleness threshold (default 20 min), the watchdog sends a nudge prompt to the thread. This is why you may see unsolicited "Have you made progress on X?" prompts.

### 9.7 Triggers

A trigger is an event → agent-run pipeline. Source types include cron schedules, webhooks, RSS feeds, incoming Discord messages, and other event buses. Each trigger has:

- A **source config** (URL, cron expression, channel filter, …).
- Optional **conditions**  -  gate the action with if/when rules matched against the event.
- An **action**  -  typically `agent_prompt` (run the agent with a templated prompt), `notify` (push a notification), or `create_todo`.
- A **cooldown** to prevent rapid re-firing.
- A **thread ID** to scope the run into.

Webhook triggers can be fired with an HTTP POST to `/triggers/fire/{trigger_id}` with an optional shared secret (no API key required  -  that path is the no-auth exception).

Health fields (`consecutive_errors`, `last_error`, `health_status`) surface as "degraded" or "failed" in the Dashboard.

### 9.8 Attachments

Every client can send multimodal files: images, PDFs, text, and code. Max 10 per message, up to 25 MB each (typical; enforced by the server). Attachments are scoped to the single message  -  they do not persist as workspace files unless a tool writes them out.

Before sending, the frontend asks the backend whether the active model supports the attachment types. Incompatible combos trigger a warning modal with "Send anyway" (the backend then receives `force_unsupported_attachments=true`). Bots auto-set that flag because they have no modal UI to surface the warning.

### 9.9 Autonomous runs

Any agent run not triggered by a live chat message  -  TODOs firing, triggers firing, watchdog nudges, self-invokes  -  streams through `/autonomous/stream`. Frontends maintain a persistent SSE connection there and route events to the matching thread's chat view so you see the run happening live even though you did not type anything. The desktop app uses fetch-based SSE with Bearer auth, reconnects dropped or idle streams, and refreshes current history/context after reconnect to fill any missed chunks.

### 9.10 Streaming event types

The same event taxonomy powers every streaming surface. These event types appear in `/chat` responses, `/autonomous/stream`, every frontend's chat container, the Discord/Telegram tool-display toggle, and the CLI stream renderer.

**`/chat` events:**

| Type | Fields | Meaning |
|------|--------|---------|
| `thinking` | `content` | Model reasoning / extended thinking |
| `tool_call` | `name`, `args`, `id` | Tool invocation beginning |
| `tool_result` | `id`, `name`, `result` | Tool finished |
| `workspace_artifact` | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` | Downloadable file was produced |
| `dispatched` | `target_thread_id`, `title`, `dispatched_to` | Leading `@thread` mention routed this turn to another thread. Clients attach a `Response from <thread>` reference line to the active assistant turn and keep normal stream rendering for thinking, responses, tools, and artifacts. |
| `response` | `content` | Visible assistant text chunk. Can arrive before a tool call as preamble/commentary or after tools as the final answer. |
| `context_attached` | `message` | Previous compaction summary attached to this turn |
| `compacted` | `messages_removed`, `auto_resumed` | Auto-compaction fired mid-run |
| `command_result` | `command`, `result` | A slash command like `/compact` was processed |
| `queued` |  -  | Run queued behind an earlier autonomous task |
| `iteration_limit` | `content`, `reason`, `max_iterations`, `tool_call_count`, optional repeated-tool fields | Agent hit the turn safety budget or repeated tool/result loop detector |
| `error` | `content` | Error |
| `done` |  -  | Stream complete |

**`/autonomous/stream` events:** same set plus `task_started` and `task_completed` book-ending each autonomous run, and periodic `: heartbeat` SSE comments to keep the connection alive.

---

## 10. REST / SSE API

**Base URL:** whatever the admin set (defaults to `http://localhost:8000`).

**Auth:** `Authorization: Bearer <API_KEY>` header on every request. Exceptions: `GET /health`, `POST /triggers/fire/{id}` (uses optional per-trigger shared secret instead).

**Error format:** `{"detail": "message"}` with standard HTTP status codes (`401`, `422`, `429`, `500`).

### 10.1 Chat

| Endpoint | Purpose |
|----------|---------|
| `POST /chat` | Streaming chat (SSE). Body: `{ message, thread_id?, user_id?, attachments?, force_unsupported_attachments?, is_self_invoke?, trigger_override?, trigger_id?, trigger_name? }`. Response: SSE event stream (see §9.10). |
| `POST /chat/sync` | Same body as `/chat`, non-streaming. Response: `{ response, thread_id, tool_call_count }`. |
| `GET /autonomous/stream?user_id=&client_id=` | SSE stream of autonomous-run events for a user. Desktop and mobile use `fetch()` streaming with `Authorization: Bearer <token>` and `Accept: text/event-stream`; `api_key` in the query string is legacy fallback only. |

### 10.2 Threads

| Endpoint | Purpose |
|----------|---------|
| `GET /threads?user_id=` | List threads. |
| `PATCH /threads/{id}/metadata` | Rename / pin. |
| `GET /threads/{id}/history` | Full message log. |
| `GET /threads/{id}/context` | Token-usage stats. |
| `POST /threads/{id}/compact` | Manual compaction. |
| `DELETE /threads/{id}` | Delete thread (history + config). |
| `POST /threads/{id}/stop` | Abort the current run (cascades to callable child threads). |
| `GET /threads/{id}/config` / `PATCH /threads/{id}/config` | Per-thread config: system prompt, tools, skills, LLM override, callable registration. |

### 10.3 TODOs

| Endpoint | Purpose |
|----------|---------|
| `GET /todos?user_id=&filter_status=&thread_id=` | List. |
| `POST /todos` | Create. |
| `PATCH /todos/{id}` | Update (task, status, schedule, recurrence, thread, or clear flags). |
| `POST /todos/{id}/complete` | Mark done; reschedule if recurring. |
| `DELETE /todos/{id}` | Delete. |

### 10.4 Triggers

| Endpoint | Purpose |
|----------|---------|
| `GET /triggers?user_id=&enabled_only=&thread_id=` | List. |
| `POST /triggers` | Create. |
| `GET /triggers/sources/list` | List available source types and their config schemas. |
| `GET /triggers/{id}/executions` | Execution history. |
| `POST /triggers/{id}/test` | Dry-run with a sample event. |
| `POST /triggers/fire/{id}?secret=` | Webhook entry point. Parses POST body as the event. No auth header. Per-trigger shared secret via `?secret=`. |

### 10.5 Settings, models, tools, skills

| Endpoint | Purpose |
|----------|---------|
| `GET /settings` / `PATCH /settings` | Global settings. PATCH triggers hot-reload. |
| `GET /settings/llm/runtime` | Diagnostic: active provider, effective max tokens, key status. |
| `GET /settings/env` | Environment variables (secrets masked; retired `NYMERIA_API_KEY` omitted). |
| `GET /settings/env/{key}` | Single env var. Secret-named values are masked unless `?reveal=true` (an explicit, audit-logged admin reveal); retired `NYMERIA_API_KEY` returns 404. |
| `GET /models` | Cached list of known OpenRouter models. |
| `GET /models/available` | Live fetch from the active provider. |
| `GET /tools` | Tools for the current thread. |
| `GET /tools/optional` | Optional tools list. |
| `GET /tools/defaults` / `PUT /tools/defaults` / `DELETE /tools/defaults` | Default tool set for new threads. |
| `GET /tools/categories` | Categorization. |
| `POST /tools/custom` / `GET /tools/custom/{id}` / `PUT /tools/custom/{id}` / `DELETE /tools/custom/{id}` | Custom HTTP/MCP tools. |
| `POST /tools/custom/{id}/test` | Dry-run a custom tool. |
| `GET /tools/custom/export` / `POST /tools/custom/import` | Backup / restore. |
| `GET /users/{user_id}/tools/unified` | Unified list (built-in + custom). |
| `GET /mcp-servers` / `POST /mcp-servers` / `PUT /mcp-servers/{id}` / `DELETE /mcp-servers/{id}` | MCP registration. |
| `POST /mcp-servers/{id}/discover` | Re-discover tools on a server. |

### 10.6 Memories & RAG

| Endpoint | Purpose |
|----------|---------|
| `GET /users/{user_id}/memories` / `POST /users/{user_id}/memories` / `DELETE /users/{user_id}/memories/{key}` | CRUD. |
| `GET /users/{user_id}/memories/search?query=` | Search. |
| `GET /users/{user_id}/rag/settings` / `PUT /users/{user_id}/rag/settings` | RAG toggle + inclusion flags. |
| `GET /users/{user_id}/rag/stats` | Index size and counts. |
| `POST /users/{user_id}/rag/reindex` / `DELETE /users/{user_id}/rag/index` | Rebuild / wipe. |

### 10.7 Workspace, voice, notifications, health

| Endpoint | Purpose |
|----------|---------|
| `GET /workspace/download?path=` | Download a file written by `file_write(..., attach=True)`. Restricted to `NYMERIA_WORKSPACE_DIR`. |
| `POST /voice/chat` | Audio in → STT → agent → TTS → audio out. |
| `POST /voice/tts` | Text → audio. |
| `POST /voice/stt` | Audio → text. |
| `GET /activity?user_id=&limit=` | Activity log. |
| `GET /notifications` / `POST /notifications/{id}/read` / `POST /notifications/read-all` | Notifications. |
| `GET /health` | Health check (unauthenticated). |
| `POST /report` | Submit an in-app error report (Outlook add-in uses this). |

### 10.8 Callable threads

| Endpoint | Purpose |
|----------|---------|
| `GET /agents/threads` | List callable threads. |
| `POST /agents/threads` | Create a callable thread. |
| `GET /agents/templates` | Templates for common callable-thread patterns. |

---

## 11. Parity Matrix

Quick answer to "Is feature X available in Y?"

| Feature | Desktop | Mobile | Discord | Telegram | Twitch ⚠️ | Outlook | CLI | API |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Send chat | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Stream / thinking / tool-call live rendering | ✅ | ✅ | ✅ (edit-in-place) | ✅ (new bubbles) | agent-driven | ✅ | ✅ | ✅ (SSE) |
| Abort run | ✅ | ✅ | ✅ (`/stop`) | ✅ (inline keyboard) | ✅ (`!stop`, mods) | ✅ | `Ctrl+C`, `/thread stop` | `POST .../stop` |
| Compact context | ✅ | ✅ | ✅ | ✅ |  -  | ✅ | ✅ | ✅ |
| Attachments (inbound) | ✅ | ✅ (+ camera) | DMs / @mentions only | ✅ | ✖ | ✅ | ✅ (non-leading `@file` in full-screen/Rich CLI) | ✅ |
| Multi-select threads | ✅ | ✖ | N/A (one per channel) | N/A | N/A | ✅ | ✖ | N/A |
| Thread rename / pin | ✅ | ✅ | N/A | N/A | N/A | ✅ | ✅ | `PATCH .../metadata` |
| Switch LLM model globally | ✅ | ✅ | `/model global` | `/model global` |  -  | ✅ | ✅ | `PATCH /settings` |
| Switch LLM model per-thread | ✅ | ✅ | `/model thread` | `/model thread` |  -  | ✅ | `/model set` | `PATCH .../config` |
| Per-thread tool overrides | ✅ | ✅ | `/tools enable\|disable` | `/tools_enable\|_disable` |  -  | ✅ | `/tools enable\|disable` | `PATCH .../config` |
| Skills (install / toggle) | ✅ (marketplace) | toggle only |  -  |  -  |  -  | ✅ | ✅ | via thread config |
| Create triggers | ✅ (wizard) | ✅ |  -  |  -  |  -  | ✅ | ✅ | `POST /triggers` |
| Create TODOs | ✅ | ✅ | `/todos add` | `/todo_add` |  -  | ✅ | `/todo add` | `POST /todos` |
| Memories | agent prompt | agent prompt | `/memory …` | `/memory_…` |  -  | agent prompt | `/memory …` | `/users/.../memories*` |
| Notepad | agent prompt | agent prompt | `/notepad …` | `/notepad_…` |  -  | agent prompt |  -  | via agent |
| Export conversation |  -  |  -  | `/export` | `/export` |  -  |  -  | `/export` | `GET .../history` |
| Restart API |  -  |  -  | `/restart api` | `/restart api` |  -  |  -  |  -  |  -  |
| Custom HTTP / MCP tools | ✅ | ✖ (view only) |  -  |  -  |  -  | ✅ | MCP management + custom-tool test | `/tools/custom`, `/mcp-servers` |
| Outlook email tools | ✅ (agent) | ✖ | ✅ (agent) | ✅ (agent) |  -  | ✅ | ✅ | via agent |

---

## 12. Known Limitations and Common Issues

### Desktop / mobile
- No in-chat voice/microphone UI today. TTS/STT settings exist but live voice interaction requires direct calls to `/voice/*` endpoints.
- No slash-command menu in the chat composer. `/compact` works because the backend recognizes it, but there is no autocomplete and no list of available slash commands in the UI.
- Over ~25 enabled tools, prompt tokens per turn grow noticeably. Disable unused optional tools per thread or globally.
- Browser-native find-in-page works inside the chat, but there is no dedicated message-search UI.
- **MCP server setup is stdio-only in the manual UI.** The Add/Edit form has no transport-type selector; SSE, HTTP, and WebSocket MCP servers cannot be added manually from the Settings panel today even if the backend supports them. Discovery and test results also have limited feedback  -  if an MCP server misbehaves, check the backend logs. Default MCP tool toggles live in Settings → MCP, and per-thread MCP tool overrides live in Thread Settings → MCP.
- **Trigger cooldowns are in seconds, not minutes.** Users who type `5` expecting five minutes will get five seconds.
- **Trigger condition logic is AND-only.** The UI has no OR operator and no grouping; a condition-set is a flat all-must-match list.

### Discord
- The `/config set` and `/env set` commands are **not** restricted to admins or the guild owner in the current build. Any user who can invoke the slash command can change backend settings. If this matters to you, ask your admin to lock down command permissions at the Discord integration level.
- `/ask` does not accept attachments (Discord slash-command limitation). Use `@mention` or DM to send files.
- Response mode is set by the admin; users cannot toggle it from Discord itself.
- 2000-char hard limit per message; long replies are split at paragraph / line / sentence boundaries. Code fences are preserved.
- Slash-command changes are near-instant for guild commands; if they don't show, close and reopen Discord's slash menu.
- `/todos complete` errors if the partial ID matches 0 or 2+ TODOs. Provide a longer prefix if ambiguous.

### Telegram
- In groups, Telegram's bot privacy mode hides normal messages from the bot. The bot sees only explicit `/commands` and replies to its own messages. To disable that, the bot admin must use BotFather → `/setprivacy` → Disable.
- HTML formatting quirks: if the LLM emits malformed `<tag>` pairs, the bot automatically re-sends as plain text. If your replies are missing bold/italic formatting, that fallback is why.
- Captions (not plain-text bodies) are used as the prompt when an attachment is sent. Messages with attachments but no caption are prompted with `[attachment]`.
- 4096-char per-message limit; splitting rules are the same as Discord.

### Twitch ⚠️
- The bot is transitional. Treat all documentation that names specific `!commands` or `twitch_*` tools as subject to change.

### Outlook add-in
- The "Add from file" manifest-sideload option is missing in the new Outlook desktop UI. Sideload via Outlook Web first.
- `confirm()` dialogs return `false` silently inside the Office.js webview. Any custom confirmation prompt the add-in uses must be a Svelte modal, not a browser-native `confirm`.
- EWS item IDs (what Office.js exposes) differ from Graph message IDs. The add-in captures subject + sender + date alongside the EWS ID so tools can search robustly.
- If the taskpane webview closes mid-stream the SSE may drop; the frontend polls `/threads/{id}/history` as a safety net during autonomous runs.

### CLI
- Interactive full-screen/Rich modes require a TTY. Piped or redirected output falls back to plain mode.
- The Rich REPL is the default interactive renderer. Full-screen mode is available with `--renderer full`; plain mode is the safest fallback for CI, pipes, and `TERM=dumb`.
- `/thread delete` (also available through `/threads delete` or `/t delete`) will not let you delete the thread you are currently in. Switch away first.
- In API mode, disconnecting the terminal does not necessarily stop backend generation. Use `Ctrl+C`, the full-screen stop path, or `/thread stop` to call the stop endpoint.

### API
- `/autonomous/stream` accepts normal `Authorization: Bearer <token>` auth. Desktop and mobile use fetch-based streaming so tokens stay out of URLs; `api_key` in the query string is only a legacy fallback for clients that cannot set headers.
- `POST /triggers/fire/{id}` is the one endpoint that does not require the API key; it uses a per-trigger shared secret instead.
- `/workspace/download` only serves files under `NYMERIA_WORKSPACE_DIR`; path traversal is blocked.

---

## 13. Glossary

| Term | Definition |
|------|-----------|
| **Agent** | The LangGraph ReAct loop in the Nymeria backend that drives responses. |
| **Attachment** | A file sent alongside a message. Scoped to the message, not persistent unless a tool writes it out. |
| **Auto-compact** | Context-management mode that summarizes older turns when the context window fills. |
| **Autonomous run** | An agent run not initiated by a live chat turn (TODOs firing, triggers firing, watchdog nudges, self-invokes). Streams through `/autonomous/stream`. |
| **Callable thread** | A thread that has been registered as a tool so other threads can invoke it. |
| **CLIProxy** | A localhost proxy (default port 8317) that terminates Claude/OpenAI subscription-tier OAuth and forwards requests with the right credentials. Desktop-only. |
| **Compaction** | Replacing older turns with a summary to free tokens. Manual via `/compact`, or automatic at the compact threshold. |
| **Context window** | The per-model token budget for in-flight conversation state. |
| **Core tool** | A tool always available to the agent by default. |
| **Custom HTTP tool** | A user-defined tool that calls a templated HTTP endpoint. Created via desktop Settings → Tools. |
| **Extended thinking** | Reasoning models' extra-tokens-before-answering feature. Toggle on / off / low / medium / high. |
| **MCP server** | Model Context Protocol server  -  an external process exposing tools the agent can call. Add in Settings → MCP. |
| **Memory** | Persistent user-scoped key/value fact available in any thread. |
| **Notepad** | Per-thread free-text area that survives context compaction; lives outside the rolling window. |
| **Optional tool** | A tool that is off by default and must be explicitly enabled per thread or in the global defaults. |
| **Pulse** (Twitch only ⚠️) | Periodic autonomous evaluation where the bot feeds unseen chat to the agent. Transitional. |
| **ReAct** | The reason-and-act loop pattern underlying the LangGraph agent. |
| **Respond mode** (Discord) | Whether the bot replies to every message (`all`) or only when @-mentioned (`mention`). Admin-set. |
| **Self-invoke** | The agent invoking itself, either through a watchdog nudge or a chained tool. Rate-limited by max-self-invokes-per-hour. |
| **Skill** | A SKILL.md bundle the agent loads progressively  -  name/description upfront, body on demand. |
| **Sliding window** | Context-management mode that drops the oldest turns rather than summarizing. |
| **SSE** | Server-Sent Events  -  the streaming protocol used by `/chat` and `/autonomous/stream`. |
| **Thread** | One conversation with Nymeria; the unit of history + config + TODOs + notepad. |
| **TODO** (frontend "Task") | Scheduled prompt the agent runs at a future time, optionally recurring. |
| **Trigger** | Event-driven rule that runs the agent when an external event occurs. |
| **Watchdog** | Background worker that nudges stale in-progress TODOs after the staleness threshold. |
| **Workspace artifact** | A file produced by `file_write(..., attach=True)`; served via `/workspace/download` and renderable in the chat via the Workspace Artifact modal. |
