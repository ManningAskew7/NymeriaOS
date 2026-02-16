# Nymeria Desktop

A modern desktop UI for the Nymeria AI agent, built with Tauri 2.x and Svelte 5.

## Features

- **Three-Panel Layout**: Sidebar (threads), Main Chat, and Dashboard (Tasks + Activity)
- **Real-time SSE Streaming**: Watch responses stream in as they're generated
- **Tool Call Visualization**: See tool calls with arguments and results in collapsible cards
- **Theme Presets**: 5 built-in themes (Midnight, Monokai, Dracula, Light, High Contrast)
- **Smart Thread Management**:
  - Auto-generated titles from the first message (no extra LLM calls)
  - Manual rename via inline editing (hover over thread, click pencil icon)
  - Collapsible folders for organizing threads (right-click to rename/delete)
  - Multi-select with Ctrl+Click / Shift+Click for bulk group or delete
  - 5 sort modes: Recent, Oldest, A-Z, Most Tasks, Active First
  - Unfiled threads grouped by date (Today, Yesterday, Previous 7 Days, Older)
- **Advanced LLM Settings**: Fine-tune model parameters (top_p, top_k, penalties, reasoning effort)
- **Persistent Configuration**: API settings and theme preferences saved locally
- **First-Run Setup Wizard**: Guided configuration for new users

## Screenshots

```
┌──────────────┬────────────────────────────────┬──────────────┐
│   SIDEBAR    │         MAIN CHAT              │  DASHBOARD   │
│   (280px)    │        (flex-grow)             │   (320px)    │
│              │                                │              │
│ [+ New Chat] │  ┌──────────────────────────┐  │  ▼ Tasks (3) │
│ [Sort: ▼]    │  │ User message (blue tint) │  │  IN PROGRESS │
│              │  └──────────────────────────┘  │  └─ Task 1   │
│ ▼ Research 2 │                                │  UPCOMING    │
│ ├─ Thread 1  │  ┌──────────────────────────┐  │  ├─ Task 2 ⏱ │
│ └─ Thread 2  │  │ ⚡ Tool: file_read   [▼] │  │  └─ Task 3   │
│ Today        │  │   Arguments: {...}       │  │  COMPLETED   │
│ └─ Thread 3  │  │   Result: "..."          │  │  └─ Task 4 ✓ │
│              │  └──────────────────────────┘  │              │
│              │                                │  ▶ Activity  │
│              │  ┌──────────────────────────┐  │              │
│              │  │ AI response w/ markdown  │  │              │
│              │  └──────────────────────────┘  │              │
│              │                                │              │
│              │  ┌──────────────────────────┐  │              │
│              │  │ Type message...   [Send] │  │              │
│              │  └──────────────────────────┘  │              │
└──────────────┴────────────────────────────────┴──────────────┘
```

## Prerequisites

- [Node.js](https://nodejs.org/) 18+
- [Rust](https://rustup.rs/) (for Tauri)
- [Nymeria API](../nymeria/) running on `http://localhost:8000`

## Getting Started

1. **Install dependencies:**
   ```bash
   npm install
   ```

2. **Start the Nymeria API** (in a separate terminal):
   ```bash
   cd ../Nymeria
   python run.py api
   ```

3. **Run in development mode:**
   ```bash
   npm run tauri dev
   ```

4. **First-Run Setup Wizard:**

   On first launch, the app displays a setup wizard that guides you through:
   - Configuring the backend URL (defaults to `http://localhost:8000`)
   - Entering your API key (the `NYMERIA_API_KEY` from Nymeria's `.env`)
   - Testing the connection

   After completing setup, you'll be taken directly to the chat interface.

5. **Manual Configuration (if needed):**
   - Click the ⚙️ settings icon in the sidebar
   - Enter API URL: `http://localhost:8000`
   - Enter API Key: your `NYMERIA_API_KEY` value
   - Click "Test Connection" to verify

## Settings

The Settings panel (⚙️ in sidebar) provides four configuration tabs:

### Connection
- API URL and key configuration
- Connection testing

### Appearance
- Theme selection with visual previews
- Changes apply instantly

### LLM
- Provider selection (Anthropic, OpenAI, OpenRouter)
- Model selection with custom model ID support
- Temperature slider
- **Advanced Settings** (collapsible):
  - Max Tokens: Limit output length
  - Top P: Nucleus sampling threshold
  - Top K: Vocabulary limiting per step
  - Frequency Penalty: Reduce repetition
  - Presence Penalty: Encourage new topics
  - Reasoning Effort: For reasoning models (Low/Medium/High)

### Agent
- Context window cycles
- Self-invoke rate limiting
- Watchdog configuration
- Log level

## Building for Production

```bash
npm run tauri build
```

Outputs:
- Windows: `.msi` installer
- macOS: `.dmg` bundle
- Linux: `.AppImage`

## Tech Stack

| Component | Technology |
|-----------|------------|
| Desktop Framework | Tauri 2.x |
| Frontend | Svelte 5 + SvelteKit |
| Build Tool | Vite |
| Styling | CSS Variables (5 theme presets) |
| Markdown | marked.js |
| Syntax Highlighting | highlight.js |

## Project Structure

```
nymeria-desktop/
├── src/
│   ├── lib/
│   │   ├── components/
│   │   │   ├── layout/        # AppShell, Sidebar, MainPanel, RightPanel
│   │   │   ├── chat/          # ChatContainer, MessageBubble, ToolCallCard, InputBar, StreamingText
│   │   │   ├── threads/       # ThreadList, ThreadItem, FolderItem
│   │   │   ├── todos/         # TodoFeed, TodoItem (collapsible with countdown badges)
│   │   │   ├── dashboard/     # ActivityFeed, ActivityItem
│   │   │   ├── notifications/ # NotificationCenter, NotificationItem
│   │   │   └── common/        # Button, Icon, Spinner, Collapsible, Modal, SettingsPanel, SetupWizard
│   │   ├── services/
│   │   │   └── api.svelte.ts      # REST + SSE streaming client
│   │   ├── stores/                # Svelte 5 runes state
│   │   │   ├── chat.svelte.ts     # Chat messages and streaming
│   │   │   ├── threads.svelte.ts  # Conversation threads, folders, sort mode
│   │   │   ├── config.svelte.ts   # API configuration + theme + setup state
│   │   │   ├── todos.svelte.ts    # TODO items (includes scheduledTodos for dashboard)
│   │   │   ├── activity.svelte.ts # Activity log feed (polls every 30s)
│   │   │   ├── autonomous.svelte.ts # SSE connection for autonomous task events
│   │   │   └── notifications.svelte.ts # Notification center
│   │   ├── themes.ts              # Theme definitions and applicator
│   │   └── types/                 # TypeScript definitions
│   ├── routes/
│   │   └── +page.svelte
│   ├── app.css                    # CSS variables + global styles
│   └── app.html
├── src-tauri/                     # Rust backend
│   ├── src/main.rs
│   └── tauri.conf.json
├── package.json
└── vite.config.js
```

## Theme Presets

The app includes 5 built-in themes, selectable from Settings > Appearance:

| Theme | Background | Accent | Description |
|-------|------------|--------|-------------|
| **Midnight** | #121417 | Cyan | Default dark theme |
| **Monokai** | #272822 | Orange/Pink | Classic editor theme |
| **Dracula** | #282a36 | Purple | Popular dark theme |
| **Light** | #ffffff | Blue | Daylight use |
| **High Contrast** | #000000 | Yellow | Accessibility focused |

Themes are applied instantly without requiring a restart. Your selection persists across sessions.

## Color Palette (Midnight Theme)

```css
/* Backgrounds */
--bg-base: #121417;
--bg-elevated: #1a1d21;
--bg-elevated-2: #22262b;

/* Text */
--text-primary: #e8eaed;
--text-secondary: #9aa0a6;

/* Accent (Blue/Cyan) */
--accent-primary: #22d3ee;
--accent-secondary: #3b82f6;

/* Semantic */
--success: #34d399;
--warning: #fbbf24;
--error: #f87171;
```

## SSE Event Types

The desktop app handles SSE events from two endpoints:

### Interactive Chat (`/chat`)
Events from user-initiated conversations:

| Event | Description |
|-------|-------------|
| `thinking` | Agent reasoning (shown as status) |
| `tool_call` | Tool invocation with name and arguments |
| `tool_result` | Tool execution result |
| `response` | Streamed response text |
| `error` | Error messages |
| `done` | Stream complete (includes `muted` flag for visibility control) |

### Autonomous Tasks (`/autonomous/stream`)
Events from scheduled TODO execution (via `autonomousStore`):

| Event | Description |
|-------|-------------|
| `task_started` | Scheduled TODO execution begins |
| `thinking` | Agent reasoning during autonomous execution |
| `tool_call` | Tool invocation with id, name, and arguments |
| `tool_result` | Tool execution result |
| `response` | Streamed response text chunks |
| `task_completed` | Execution finished (includes `visibility`, `notify`, `content`) |

**Visibility Control:**
- `visibility: "full"` → Response shown in chat thread
- `visibility: "activity"` → Response hidden from chat, logged to activity feed only

## Architecture

### Autonomous Task Streaming

The `autonomousStore` maintains a persistent SSE connection to receive real-time updates when the backend executes scheduled TODOs:

```
Backend Ticker                    Frontend
     │                                │
     │ executes scheduled TODO        │
     ▼                                │
  EventBus.publish()                  │
     │                                │
     └──► /autonomous/stream ─────────► autonomousStore
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              chatStore         todosStore       activityStore
           (streaming msg)     (refresh list)   (refresh feed)
```

**Connection Flow:**
1. On app mount, `configStore.isConfigured` is checked
2. If configured, SSE connection established after 500ms delay
3. API key passed as query parameter (EventSource limitation)
4. Heartbeats every 1s keep connection alive
5. Auto-reconnect on failure (max 10 attempts, exponential backoff)

**Event Routing:**
- `task_started` → Creates placeholder message in chat, refreshes todos
- `tool_call/tool_result` → Updates tool cards in streaming message
- `response` → Appends content to streaming message
- `task_completed` → If `visibility="activity"`, removes message from chat

### Config Store & Setup Flow

The `configStore` manages API configuration and tracks setup completion:

```typescript
{
  apiUrl: string,        // Backend URL (default: http://localhost:8000)
  apiKey: string,        // API authentication key
  setupCompleted: bool,  // Has user completed first-run wizard?
  theme: ThemeName       // Current theme preset
}
```

**Migration Note:** If a user has valid config but `setupCompleted=false` (pre-existing config before the flag was added), the app auto-sets `setupCompleted=true` on load.

---

## Development Notes

### Svelte 5 Runes

State management uses Svelte 5 runes:

```typescript
// Store pattern
function createChatStore() {
  let messages = $state<Message[]>([]);
  let isStreaming = $state(false);

  return {
    get messages() { return messages; },
    addMessage(msg) { messages = [...messages, msg]; }
  };
}
```

### API Service

The API service uses async generators for SSE streaming:

```typescript
async *chatStream(message: string, threadId?: string): AsyncGenerator<SSEEvent> {
  // Yields parsed SSE events as they arrive
}
```

## License

MIT
