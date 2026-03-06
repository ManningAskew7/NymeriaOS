# Desktop vs Mobile — Cross-Platform Reference

This document maps every file in the desktop app (`/nymeria-desktop`) to its mobile counterpart (`/nymeria-mobile`) and documents the differences. Use it when replicating changes across both apps.

## Quick Reference

| Category | Desktop | Mobile |
|----------|---------|--------|
| **Framework** | Tauri 2.x + SvelteKit | Capacitor 6.x + SvelteKit |
| **Native shell** | Rust (minimal) | Android/iOS (Capacitor) |
| **Adapter** | `adapter-static` (SPA) | `adapter-static` (SPA) |
| **Svelte version** | 5.0.0 (runes) | 5.0.0 (runes) |
| **Dev port** | 1420 | 5173 |
| **Window sizing** | 1400x900, min 1000x600 | Full-screen mobile viewport |
| **Layout model** | 3-panel flexbox, collapsible sidebars | 3-panel horizontal scroll-snap |
| **Navigation** | Keyboard shortcuts (Ctrl+B, Ctrl+Shift+B) | Swipe gestures + panel dots |
| **Backend default** | `http://localhost:8000` | Empty (user configures network address) |

---

## File-by-File Comparison

### Identical Files (copy directly)

These files are byte-for-byte identical. Changes to one **must** be copied to the other.

| File (relative to `src/lib/`) | Lines | Notes |
|-------------------------------|-------|-------|
| `types/index.ts` | 737 | All TypeScript types and interfaces |
| `services/api.svelte.ts` | ~1200 | Full REST + SSE API client |
| `stores/chat.svelte.ts` | 878 | Message streaming, steps, tool calls, throttled flushing |
| `stores/threads.svelte.ts` | 669 | Thread list, folders, sorting, sync, pins |
| `stores/threadConfig.svelte.ts` | — | Per-thread LLM and tool config |
| `stores/autonomous.svelte.ts` | — | SSE connection to `/autonomous/stream` |
| `stores/todos.svelte.ts` | — | Todo CRUD, grouping, scheduling |
| `stores/health.svelte.ts` | — | Backend health polling |
| `stores/notifications.svelte.ts` | — | Notification polling |
| `stores/activity.svelte.ts` | — | Activity feed |
| `stores/models.svelte.ts` | — | Model metadata |
| `stores/tools.svelte.ts` | — | Custom tools CRUD |
| `stores/builtInTools.svelte.ts` | — | Built-in tool metadata |
| `stores/defaultTools.svelte.ts` | — | Default tool config |
| `stores/unifiedTools.svelte.ts` | — | Merged tool list |
| `stores/triggers.svelte.ts` | — | Trigger management |
| `stores/navigation.svelte.ts` | — | Navigation helpers |
| `themes.ts` | 310 | 5 themes, CSS variable system |
| `utils/markdown.ts` | 101 | marked + highlight.js + remend |
| `utils/modelOptions.ts` | 34 | Model parameter helpers |
| `components/chat/MessageBubble.svelte` | 731 | Message rendering (steps, tools, thinking, attachments) |
| `components/chat/ChatContainer.svelte` | — | Scrollable message list, auto-scroll |
| `components/chat/ToolCallCard.svelte` | — | Tool invocation display |
| `components/chat/ThinkingBlock.svelte` | — | Collapsible thinking content |
| `components/chat/StreamingText.svelte` | — | Blinking cursor + markdown |
| `components/chat/ContextStatusBar.svelte` | — | Token usage display |
| `components/chat/FilePreview.svelte` | — | File attachment preview |
| `components/chat/ImageModal.svelte` | — | Full-size image viewer |
| `components/common/Button.svelte` | — | Styled button variants |
| `components/common/Icon.svelte` | — | SVG icon library |
| `components/common/Modal.svelte` | — | Overlay modal |
| `components/common/Spinner.svelte` | — | Loading indicator |
| `components/common/Collapsible.svelte` | — | Expand/collapse container |
| `components/common/ThinkingIndicator.svelte` | — | Animated thinking state |
| `components/notifications/NotificationCenter.svelte` | — | Notification dropdown |
| `components/notifications/NotificationItem.svelte` | — | Individual notification |
| `components/dashboard/ActivityFeed.svelte` | — | Activity log display |
| `components/dashboard/ActivityItem.svelte` | — | Individual activity entry |
| `components/dashboard/ScheduledTasksFeed.svelte` | — | Scheduled todos countdown |
| `components/dashboard/ScheduledTodoItem.svelte` | — | Individual scheduled item |
| `components/tools/ToolManagementPanel.svelte` | — | Tool enable/disable list |
| `components/tools/ToolCountWarning.svelte` | — | Tool count warning |
| `components/triggers/TriggerConfigTab.svelte` | — | Trigger configuration |

> **Important**: The threads store is identical even though mobile's ThreadList UI doesn't use folders, pins, or sorting. The full capability is available — the mobile UI just hasn't exposed it yet.

---

### Files with Minor Differences

#### `stores/config.svelte.ts`

**Only difference**: default `apiUrl` value.

| | Desktop | Mobile |
|-|---------|--------|
| Default `apiUrl` | `'http://localhost:8000'` | `''` (empty) |
| Reset `apiUrl` | `'http://localhost:8000'` | `''` |

**When changing**: If you modify any logic in this store, copy it to both. Only preserve the `apiUrl` default difference.

#### `routes/+layout.ts`

Functionally identical — both export `ssr = false`. Only the comment differs (Tauri vs Capacitor).

---

### Files with Significant Differences

These files share core logic but have platform-specific adaptations. When making changes, you need to understand what's shared vs. what's platform-specific.

#### `stores/ui.svelte.ts` — Completely Different

| Aspect | Desktop | Mobile |
|--------|---------|--------|
| **Paradigm** | Binary collapse: `sidebarCollapsed`, `rightPanelCollapsed` | 3-panel swipe: `activePanel` (`'left' \| 'chat' \| 'right'`) |
| **Navigation** | `toggleSidebar()`, `toggleRightPanel()` | `goToPanel()`, `goToChat()`, `syncFromScroll()` |
| **Storage key** | `nymeria-ui` | `nymeria-ui-mobile` |
| **Extra state** | None | `keyboardVisible`, `keyboardHeight`, `scrollContainer` ref |
| **CSS integration** | None | Sets `--keyboard-height` CSS variable |

**When changing**: These are independent implementations. Changes to one rarely need replication.

#### `components/chat/InputBar.svelte`

**Same core**: `inputValue`, `pendingFiles`, file validation, `addFiles()`, `removeFile()`, paste handler, `FilePreview`/`ImageModal` sub-components, error banner.

| Aspect | Desktop | Mobile |
|--------|---------|--------|
| **Send shortcut** | `Ctrl/Cmd + Enter` | `Enter` (Shift+Enter for newline) |
| **File input** | Drag-and-drop + file picker + paste | Camera + file picker + paste (no drag-and-drop) |
| **Camera** | Not available | `@capacitor/camera` integration |
| **Haptics** | None | `hapticImpact('light')` on send |
| **Textarea max height** | 200px | 120px |
| **Font size** | `var(--font-size-base)` | `16px` (prevents iOS auto-zoom) |
| **Send button** | Rectangle `<Button>` component | Circle (44x44px) with scale animation |
| **Layout order** | Textarea → Attach → Send | Attach + Camera → Textarea → Send |
| **Container style** | Glassmorphism + focus glow | Flat `border-top`, solid background |
| **Keyboard padding** | None | `padding-bottom: var(--keyboard-height)` |
| **Hint text** | "Press Ctrl+Enter to send" row | None |
| **Drop overlay** | Present | Not present |
| **Touch targets** | Standard | 44px minimum |

**When changing**: Changes to file processing logic, the `addFiles`/`removeFile` functions, or the file validation flow should be replicated. UI layout and interaction changes are platform-specific.

#### `components/common/SettingsPanel.svelte`

Desktop: 1129 lines. Mobile: 957 lines. Same settings categories (Connection, Appearance/Theme, LLM, Agent, Tools).

| Aspect | Desktop | Mobile |
|--------|---------|--------|
| **Rendering** | Inline panel (no props) | Full-screen modal (`open` + `onClose` props) |
| **Header** | Tab bar only | Back button + title + tab bar |
| **Tab labels** | "Appearance" | "Theme" |
| **Theme grid** | `auto-fill, minmax(140px, 1fr)` | Fixed `repeat(2, 1fr)` |
| **Model help** | Info button with step-by-step guide | Not present |
| **Touch targets** | Standard sizes | Min-height 44-48px |
| **Range sliders** | Default | Custom 24px thumbs |
| **Safe areas** | None | `env(safe-area-inset-*)` padding |
| **On save connection** | No side effects | Calls `healthStore.check()` + sets `setupCompleted` |
| **Active class** | `.selected` | `.active` |

**When changing**: Settings fields, validation logic, and API call structure should be replicated. Layout, sizing, and mobile UX are platform-specific.

#### `components/common/SetupWizard.svelte`

Both have 4-step onboarding (Welcome → URL → API Key → Complete). The mobile version adds larger touch targets, safe-area padding, and tests the connection on save. Otherwise the same flow.

#### `components/threads/ThreadList.svelte`

Desktop: 725 lines. Mobile: 145 lines. **Most divergent file.**

| Feature | Desktop | Mobile |
|---------|---------|--------|
| **Folders** | Full folder CRUD, drag into folders | Not implemented |
| **Multi-select** | Ctrl+Click, Shift+Click range | Not implemented |
| **Bulk actions** | Bulk delete, bulk group | Not implemented |
| **Sort modes** | 5 modes (recent, oldest, A-Z, tasks, active) | None (always recent) |
| **Inline rename** | Double-click to rename | Not implemented |
| **Pin threads** | Pin/unpin with pinned group | Not implemented |
| **Badges** | Callable, custom-config indicators | Not implemented |
| **Task counts** | Shows active task count | Not implemented |
| **Search** | Not present | Text search filter at top |
| **Thread config** | Configure button → ThreadSettingsPanel modal | Not present (uses separate route) |

**When changing**: Adding new thread list features requires independent implementation on each platform. The underlying `threadsStore` is shared, so data-layer changes sync automatically.

#### `components/threads/ThreadItem.svelte`

Desktop version has inline rename, modifier-key click handling (Ctrl/Shift for multi-select), callable/config badges. Mobile version is simplified with just select + delete callbacks.

#### `components/threads/ThreadSettingsPanel.svelte`

Both exist and provide per-thread LLM config UI. The mobile version has larger touch targets and full-screen modal presentation.

#### `routes/+page.svelte` — Main Entry Point

**Same core**: Setup wizard check, thread sync from backend, chat history loading, autonomous stream connection.

| Aspect | Desktop | Mobile |
|--------|---------|--------|
| **Shell** | `<AppShell>` with 3 snippet props | `<MobileShell />` (no props) |
| **Lifecycle** | None | `initLifecycle()` / `destroyLifecycle()` |
| **Back button** | N/A | Android back button handler |
| **App state** | N/A | Foreground/background handling (pause/resume health, autonomous, preferences backup) |
| **Preferences** | localStorage only | localStorage + Capacitor Preferences backup |
| **Health polling** | Not started here | `healthStore.startPolling()` on mount |
| **Notification polling** | Not started here | `notificationStore.startPolling()` on mount |
| **Thread restore** | Complex: validates platform, falls back | Simple: direct load |
| **SSE delay** | 500ms delay before connecting | Immediate |
| **Debug logging** | Extensive `console.log` | Minimal |

**When changing**: Changes to startup logic (thread sync, history loading, setup wizard flow) should be replicated, respecting each platform's lifecycle.

#### `app.css` — Global Styles

**Same**: All CSS variable values (colors, typography, glassmorphism, hljs theme).

| Aspect | Desktop | Mobile |
|--------|---------|--------|
| **Header height** | `48px` | `56px` |
| **Panel vars** | `--sidebar-width: 280px`, `--right-panel-width: 320px` | None (panels are `100vw`) |
| **Mobile vars** | None | `--input-bar-height: 56px`, `--touch-target-min: 48px`, `--safe-area-*`, `--keyboard-height` |
| **Body** | `height: 100vh` | `height: 100dvh`, `position: fixed` (prevents iOS bounce) |
| **Overscroll** | Default | `overscroll-behavior: none` |
| **Tap highlight** | Default | `transparent` |
| **Button sizes** | Default | Min 48px touch targets |
| **Input font-size** | `inherit` | `16px` (prevents iOS zoom) |
| **Scrollbars** | 6px, expand to 8px on hover | 4px fixed, no hover |
| **Animations** | `staggerFadeIn`, `glowPulse`, `checkBounce` | None |

**When changing**: If adding CSS variables (new colors, spacing), add to both. Mobile-specific sizing and touch adaptations are independent.

---

### Desktop-Only Files (no mobile equivalent)

| File | Purpose | Migration Notes |
|------|---------|-----------------|
| `components/layout/AppShell.svelte` | 3-panel flexbox with collapsible sidebars | Replaced by `MobileShell.svelte` |
| `components/layout/Sidebar.svelte` | Left sidebar container | Replaced by `LeftPanel.svelte` |
| `components/layout/MainPanel.svelte` | Center panel container | Replaced by `ChatPanel.svelte` |
| `components/threads/ThreadHeader.svelte` | Current thread title + platform indicator | Integrated into `ChatPanel` header |
| `components/threads/FolderItem.svelte` | Folder display in thread list | Not needed (folders not in mobile UI) |
| `components/tools/ToolForm.svelte` | Create/edit custom tool form | Not yet ported |
| `components/tools/ToolTestPanel.svelte` | Test tool with parameters | Not yet ported |
| `stores/utils/crud-store.ts` | CRUD utility pattern | Not used in mobile |
| `stores/utils/polling.ts` | Polling helper | Not used in mobile |
| `lib/index.ts` | Barrel exports | Not needed |

### Mobile-Only Files (no desktop equivalent)

| File | Purpose | Notes |
|------|---------|-------|
| `components/layout/MobileShell.svelte` | 3-panel scroll-snap container | Replaces `AppShell.svelte` |
| `components/layout/LeftPanel.svelte` | Threads + settings + notifications | Replaces `Sidebar.svelte` |
| `components/layout/ChatPanel.svelte` | Chat header + container + input | Replaces `MainPanel.svelte` |
| `utils/lifecycle.ts` | Capacitor app lifecycle (back button, foreground/background, Preferences backup) | Capacitor-specific |
| `utils/haptics.ts` | Haptic feedback wrapper (`hapticImpact`, `hapticNotification`) | Capacitor-specific |

### Component Directory Reorganization

Some components moved directories between desktop and mobile:

| Desktop Location | Mobile Location |
|-----------------|-----------------|
| `components/todos/TodoFeed.svelte` | `components/dashboard/TodoFeed.svelte` |
| `components/todos/TodoForm.svelte` | `components/dashboard/TodoForm.svelte` |
| `components/todos/TodoItem.svelte` | `components/dashboard/TodoItem.svelte` |
| `components/dashboard/ConnectionStatus.svelte` | `components/common/ConnectionStatus.svelte` |

---

## Change Replication Guide

### Adding a new API endpoint

1. Add the method to `services/api.svelte.ts` (identical file — copy to both)
2. Add any new types to `types/index.ts` (identical file — copy to both)
3. If it needs a new store, create it identically in both `stores/` directories

### Adding a new store

1. Create the store file (should be identical unless it manages UI state)
2. Import and initialize in `+page.svelte` (both, respecting lifecycle differences)
3. If it needs polling, start/stop it in the mobile `+page.svelte` lifecycle handlers

### Adding a new SSE event type

1. Add the type to `types/index.ts` (copy to both)
2. Handle it in `stores/chat.svelte.ts` or `stores/autonomous.svelte.ts` (both identical)
3. If it needs UI, add to `MessageBubble.svelte` (identical) or platform-specific components

### Modifying chat streaming logic

1. Change `stores/chat.svelte.ts` (identical — copy to both)
2. If the change affects message rendering, update `MessageBubble.svelte` (identical — copy to both)

### Adding a new theme

1. Add to `themes.ts` (identical — copy to both)
2. Update `SettingsPanel.svelte` in both (different files — update each independently)

### Adding a new settings field

1. Add the type to `types/index.ts` (copy to both)
2. Add the API call to `services/api.svelte.ts` (copy to both)
3. Add UI to `SettingsPanel.svelte` (implement in each with platform-appropriate sizing)

### Adding a new chat feature (e.g., reactions, editing)

1. Types: `types/index.ts` (copy)
2. API: `services/api.svelte.ts` (copy)
3. Store logic: `stores/chat.svelte.ts` (copy)
4. Message rendering: `MessageBubble.svelte` (copy, unless it needs touch-specific interactions)
5. Input UI: `InputBar.svelte` (implement independently — different interaction models)

### Adding thread list features (folders, sorting, pins)

The `threadsStore` already supports all features in both. Only the `ThreadList.svelte` UI differs:
- Desktop: Full-featured (725 lines)
- Mobile: Simplified (145 lines) — needs independent implementation with touch-friendly UX

---

## Mobile-Specific Patterns

When implementing anything on mobile, apply these patterns:

### Touch Targets
All interactive elements must be at least 44-48px (`--touch-target-min`).

### Safe Areas
Use `env(safe-area-inset-*)` for content near screen edges (notch, home indicator).

### Keyboard Handling
The mobile app uses `resize: 'none'` for the keyboard — it sets `--keyboard-height` CSS variable instead of letting the browser resize. Input areas should use `padding-bottom: var(--keyboard-height)`.

### Font Size
Text inputs must use `font-size: 16px` to prevent iOS Safari from auto-zooming on focus.

### Haptics
Use `hapticImpact('light')` for navigation and sends, `hapticNotification('success'|'error')` for outcomes.

### Lifecycle
Mobile apps pause/resume. The `lifecycle.ts` utility handles:
- **Background**: Stop polling, disconnect SSE, backup localStorage to Capacitor Preferences
- **Foreground**: Resume polling, reconnect SSE, restore from Preferences
- **Back button**: Navigate to chat panel first, then allow app exit

### Dynamic Viewport
Use `100dvh` not `100vh` to account for mobile browser chrome appearing/disappearing.

---

## Native Dependencies

### Desktop (Tauri)
- `@tauri-apps/api` — Window management
- `@tauri-apps/plugin-opener` — Open URLs externally
- Rust backend: Minimal (just a `greet` demo command)

### Mobile (Capacitor)
- `@capacitor/app` — Lifecycle, back button
- `@capacitor/camera` — Photo capture for attachments
- `@capacitor/keyboard` — Keyboard visibility
- `@capacitor/network` — Connectivity status
- `@capacitor/preferences` — Persistent key-value storage
- `@capacitor/splash-screen` — Startup screen
- `@capacitor/status-bar` — Status bar styling
- `@capacitor/haptics` — Haptic feedback

---

## Shared Dependencies

Both apps use these identical npm packages:
- `svelte` 5.0.0, `@sveltejs/kit` 2.9.0, `@sveltejs/adapter-static`
- `marked` 12.0.0 (markdown), `highlight.js` 11.0.0 (syntax highlighting), `remend` 1.2.1
- `typescript` 5.6.2, `vite` 6.x
