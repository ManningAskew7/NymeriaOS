# Desktop vs Mobile — Cross-Platform Reference

This document maps the major desktop app areas (`/nymeria-desktop`) to their mobile counterparts (`/nymeria-mobile`) and documents the important differences. It is a maintenance guide, not a byte-for-byte file inventory, because the two codebases have already diverged in a number of platform-specific and feature-specific areas.

**Drift checker**: `python scripts/check_cross_app_drift.py` enforces that shared files stay in sync and new overlap files are classified. It runs in CI and flags exact-match files that have drifted and unclassified new overlaps. Add new shared files to `EXACT_MATCH` or `KNOWN_DRIFT` in the script.

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

### Shared core areas

A lot of business logic is still conceptually shared across desktop and mobile, especially around chat, threads, tools, triggers, MCP, and API interaction. But these files are **no longer safe to assume byte-identical**.

In practice:
- treat shared stores and services as **parallel implementations with substantial overlap**
- compare before copying changes
- replicate logic intentionally instead of assuming a blind file copy is correct

This matters because the apps now differ in several real ways, including:
- mobile lifecycle handling (`@capacitor/app`, Preferences backup/restore, back button handling)
- mobile haptics and keyboard state
- desktop-only Tauri startup behavior and backend readiness UI
- desktop-only Outlook mode and CLIProxy integration
- different component organization in a few areas
- some store and type files already diverging in content, not just comments

> **Important**: The underlying concepts are often shared, but this document should be used as a synchronization guide, not proof that files are identical.

---

### Files with Minor Differences

#### `stores/config.svelte.ts`

The default `apiUrl` still differs by platform, but this is no longer the only difference worth assuming. Treat it as mostly-shared logic that still needs comparison before copying.

| | Desktop | Mobile |
|-|---------|--------|
| Default `apiUrl` | `'http://localhost:8000'` | `''` (empty) |
| Reset `apiUrl` | `'http://localhost:8000'` | `''` |
| Setup gate | `needsSetup` is true when setup is incomplete or URL/token is blank | Same |
| `/me` 401 handling | Clears token, setup flag, identity, and identity scope | Same |

**When changing**: replicate configuration logic carefully, while preserving platform-specific defaults and any mobile setup behavior tied to first-run connection flow.

User-scoped API helpers and stores must stay in sync across both apps. `services/api.svelte.ts` resolves optional `userId` arguments from `configStore.identity?.id`; per-user tool/skill/trigger stores register `registerIdentityReloadHook` and guard async loads with an identity generation so stale responses from the previous account cannot repopulate state after a switch.

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

Desktop: 2065 lines. Mobile: 1272 lines. Same core settings categories (Connection, Appearance/Theme, LLM, Agent, Tools, MCP). Desktop also owns global Skills management and marketplace install; mobile keeps Skills control inside per-thread settings.

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
| **Lines** | ~2065 | ~1272 |

**When changing**: Settings fields, validation logic, and API call structure should be replicated. Layout, sizing, and mobile UX are platform-specific.

#### `components/common/SetupWizard.svelte`

Both have 4-step onboarding (Welcome → URL → API Key → Complete). The mobile version adds larger touch targets, safe-area padding, and tests the connection on save. Otherwise the same flow.

#### `components/threads/ThreadList.svelte`

Desktop: 1258 lines. Mobile: 145 lines. **Most divergent file.**

| Feature | Desktop | Mobile |
|---------|---------|--------|
| **Folders / teams** | Folder CRUD plus a folder/team organization toggle; teams are backed by the callable-team API and can be created from multi-select | Not implemented |
| **Multi-select** | Ctrl+Click, Shift+Click range | Not implemented |
| **Bulk actions** | Bulk delete, bulk group | Not implemented |
| **Sort modes** | 5 modes (recent, oldest, A-Z, tasks, active) | None (always recent) |
| **Inline rename** | Double-click to rename | Not implemented |
| **Pin threads** | Pin/unpin with pinned group | Not implemented |
| **Badges** | Callable, custom-config indicators | Not implemented |
| **Task counts** | Shows active task count | Not implemented |
| **Search** | Not present | Text search filter at top |
| **Thread config** | Configure button/right-click → ThreadSettingsPanel modal; row Agent shortcut opens the Agent tab directly | Not present in list (settings open from chat header) |
| **Lines** | ~1258 | ~145 |

**When changing**: Adding new thread list features requires independent implementation on each platform. The underlying `threadsStore` is shared, so data-layer changes sync automatically.

#### `components/threads/ThreadItem.svelte`

Desktop version has inline rename, modifier-key click handling (Ctrl/Shift for multi-select), callable/config badges, and an accent-glowing Agent settings shortcut that remains visible for callable threads while staying hover/focus-only for other threads. Mobile version is simplified with just select + delete callbacks.

#### `components/threads/ThreadSettingsPanel.svelte`

Both exist and provide per-thread LLM config UI plus dedicated Agent, MCP, Skills, and Triggers tabs for callable-thread settings, MCP tool overrides, skill overrides, and trigger setup. The desktop modal shrink-wraps wider tab sets up to a viewport-capped width, with horizontal tab scrolling as the fallback for narrow windows or future tabs. The mobile version has larger touch targets and full-screen modal presentation.

Skills parity is intentionally partial on mobile: mobile can list installed skills and set per-thread enable/disable overrides, while marketplace install, uninstall, and global skill defaults remain desktop-only because those are administrative/library-management workflows.

Per-thread attention settings are shared conceptually across both apps:
`telegram_autonomous_delivery` controls whether Telegram gets full autonomous
output, explicit notifications only, or no autonomous delivery; and
`in_app_notification_level` controls whether the notification center shows only
explicit `notify` calls, all autonomous completions, or nothing for that thread.
Desktop places these controls in the Chat App tab. Mobile places them in the
Advanced section of Thread Settings.

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
| **Autonomous SSE transport** | `fetch()` + `ReadableStream`, Bearer auth header, `client_id`, idle timeout reconnect, reconnect catch-up | `fetch()` + `ReadableStream`, Bearer auth header, `client_id`, idle timeout reconnect, reconnect catch-up, legacy query-token compatibility, Capacitor Network offline/online reconnect, mobile lifecycle reconnect |
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

**When changing**: If adding CSS variables (new colors, spacing), add to both. Theme color variables must be added through the typed `themeColorCssVariables` map in both `themes.ts` files so `npm run check` fails if `ThemeColors` and the applied CSS variables drift. Mobile-specific sizing and touch adaptations are independent.

---

### Desktop-Only Files / Features (no mobile equivalent)

| File | Purpose | Migration Notes |
|------|---------|-----------------|
| `components/layout/AppShell.svelte` | 3-panel flexbox with collapsible sidebars | Replaced by `MobileShell.svelte` |
| `components/layout/Sidebar.svelte` | Left sidebar container | Replaced by `LeftPanel.svelte` |
| `components/layout/MainPanel.svelte` | Center panel container | Replaced by `ChatPanel.svelte` |
| `components/threads/ThreadHeader.svelte` | Current thread title + platform indicator; callable count badge uses the per-thread `/threads/{id}/callable-tools` endpoint | Integrated into `ChatPanel` header with the same per-thread callable count lookup |
| `components/threads/FolderItem.svelte` | Folder display in thread list | Not needed (folders not in mobile UI) |
| Thread config sharing UI | Import `.nymeria-thread.json` files from the desktop thread list and export portable config-only shares from thread context menus | Backend API exists for mobile, but mobile has no UI in v1 |
| `components/common/CLIProxyPanel.svelte` | CLIProxy management UI | Desktop-only, tied to Tauri/local proxy workflows |
| `components/common/StartupOverlay.svelte` | Backend startup/readiness overlay | Desktop-only Tauri startup behavior |
| `components/outlook/QuickActions.svelte` | Outlook-specific quick actions | Desktop-only Outlook integration |
| `components/chat/AgentActivityIndicator.svelte` | Inline animated phase text for silent assistant phases (`Processing...`, `Thinking...`, `Formulating...`, `Processing results...`, `Waiting...`); visible response chunks render directly without a separate typing label or cursor | Desktop-only v1; mobile still uses its existing dots/ThinkingIndicator flow |
| `components/tools/ToolForm.svelte` | Create/edit custom tool form | Desktop-only today |
| `components/tools/ToolTestPanel.svelte` | Test tool with parameters | Desktop-only today |
| `stores/backendProcess.svelte.ts` | Tracks embedded backend startup state | Desktop-only |
| `stores/cliproxy.svelte.ts` | CLIProxy status and controls | Desktop-only |
| `stores/connections.svelte.ts` | Connection-switching helpers | Desktop-only |
| `stores/outlook.svelte.ts` | Outlook mode state | Desktop-only |
| `stores/syncPoll.svelte.ts` | Sync/message-count polling helpers | Desktop-only |
| `lib/index.ts` | Barrel exports | Not needed |

### Mobile-Only Files / Features (no desktop equivalent)

| File | Purpose | Notes |
|------|---------|-------|
| `components/layout/MobileShell.svelte` | 3-panel scroll-snap container | Replaces `AppShell.svelte` |
| `components/layout/LeftPanel.svelte` | Threads + settings + notifications | Replaces `Sidebar.svelte` |
| `components/layout/ChatPanel.svelte` | Chat header + container + input | Replaces `MainPanel.svelte` |
| `utils/lifecycle.ts` | Capacitor app lifecycle (back button, foreground/background, Preferences backup/restore) | Capacitor-specific |
| `utils/haptics.ts` | Haptic feedback wrapper (`hapticImpact`, `hapticNotification`) | Capacitor-specific |

### Component Directory Reorganization

Some components moved directories between desktop and mobile:

| Desktop Location | Mobile Location |
|-----------------|-----------------|
| `components/todos/TodoFeed.svelte` | `components/dashboard/TodoFeed.svelte` |
| `components/todos/TodoForm.svelte` | `components/dashboard/TodoForm.svelte` |
| `components/todos/TodoItem.svelte` | `components/dashboard/TodoItem.svelte` |
| `components/dashboard/ConnectionStatus.svelte` | `components/common/ConnectionStatus.svelte` |

### Account UI (`components/account/`)

All account/identity surfaces live under `components/account/` in both apps. Most files mirror 1:1 with cosmetic mobile-touch tweaks (40px revoke buttons, larger inputs, bottom-sheet menu instead of popover). Two files are **desktop-only** because mobile is single-connection.

| Component | Desktop | Mobile | Notes |
|---|---|---|---|
| `Avatar.svelte`, `RoleChip.svelte`, `avatar.ts` | ✓ | ✓ | Identical helpers; pure functions in `avatar.ts`. |
| `AccountBadge.svelte` | Sidebar bottom-bar (full + collapsed icon) | LeftPanel footer-actions (40×40) | Replaces the old `ConnectionSwitcher` slot on desktop. |
| `AccountMenu.svelte` | Anchored popover (NotificationCenter pattern) | Bottom sheet (Modal-style) | Same items: Manage account / Manage users (admin) / Switch / Add / Sign out. |
| `AccountSwitcher.svelte` | ✓ | — | Mobile is single-connection; switching is via Settings → Connection. |
| `AddAccountSheet.svelte` | ✓ | — | Same reason. |
| `AccountTab.svelte` | ✓ | ✓ | Same sections (Identity / Tokens / Platforms (admin) / Sign out); mobile uses larger inputs. |
| `UsersTab.svelte` | ✓ (admin only tab) | ✓ (admin only tab) | Same master/detail; mobile detail view stacks form fields vertically. |
| `TokenManagementSection.svelte` | ✓ | ✓ | `mode: 'self' \| 'admin'` prop; admin mode adds Rotate-all. |
| `PlatformLinkingSection.svelte` | ✓ | ✓ | Always uses admin endpoints — only rendered for admins. |
| `CopyOnceTokenDialog.svelte` | ✓ | ✓ | Shared copy-once token modal; desktop adds optional account-switcher save actions for admin-issued tokens. |

**Supporting files (also mirror in both apps):**
- `services/api.svelte.ts` — 16 new account/admin methods + `_toastAndExtractError` helper.
- `stores/config.svelte.ts` — `signOut()`, `updateIdentityDisplayName()`.
- `stores/connections.svelte.ts` — **desktop only**; extends `SavedConnection` with cached identity + `verifyEntry()`.
- `stores/errors.svelte.ts` — toast queue + `pushAuthInvalid()`.
- `components/common/ErrorToast.svelte` — mounted at `routes/+page.svelte` root in both apps.

See [`frontend-accounts.md`](frontend-accounts.md) for the full reference.

---

## Change Replication Guide

### Adding a new API endpoint

1. Add the method to `services/api.svelte.ts` in both apps
2. Add any new types to `types/index.ts` in both apps
3. If it needs a new store, add it to both platforms unless the feature is explicitly platform-specific
4. Compare existing desktop/mobile implementations before copying because these files have already diverged

### Adding a new store

1. Create the store on both platforms if the feature is shared
2. Import and initialize it in each `+page.svelte` or shell entry point as needed
3. Respect lifecycle differences, especially mobile foreground/background handling and desktop startup readiness
4. Do not assume new stores will remain identical over time

### Adding a new SSE event type

1. Add the type to `types/index.ts` in both apps
2. Handle it in `stores/chat.svelte.ts` or `stores/autonomous.svelte.ts` on both platforms
3. Update rendering in `MessageBubble.svelte` or any platform-specific component affected
4. Verify both implementations, because these files are no longer guaranteed identical

Autonomous stream transport is intentionally not byte-identical today. Desktop's `stores/autonomous.svelte.ts` is the proven runtime path for live autonomous TODO/trigger streaming: Bearer-auth fetch stream, per-session `client_id`, heartbeat/idle guard, reconnect catch-up, and sampled console diagnostics. Mobile also uses fetch streaming with Bearer auth and a per-session `client_id` because WebView EventSource behavior is unreliable, but it still includes the legacy `api_key` query parameter for compatibility.

Current example: `workspace_artifact` is normalized in both apps' `types/index.ts` and `services/api.svelte.ts`, but only desktop renders it today via `ToolCallCard.svelte` + `WorkspaceArtifactModal.svelte`.

Compaction UX is shared across both apps: `/history` maps `kind: "compaction_notice"` plus `context_summary`, `messages_removed`, and `auto_resumed`; live `compacted` clears old visible messages, inserts the notice, and creates a fresh assistant stream slot when `auto_resumed` is true. Keep `ChatContainer.svelte`, `MessageBubble.svelte`, `stores/chat.svelte.ts`, and `services/api.svelte.ts` aligned for this flow.

TODO dashboard invalidation is also shared: live chat handlers and
`stores/autonomous.svelte.ts` use `utils/todoTools.ts` to recognize TODO tool
names (`nym_todo`, `nym_todo_delete`, legacy `todo_*`, and Nymeria MCP TODO
names) and call `todosStore.onTodoToolCompleted()`. Keep that helper and the
TODO store's current-filter-preserving refresh behavior aligned across desktop
and mobile.

### Modifying chat streaming logic

1. Update `stores/chat.svelte.ts` on both platforms
2. If rendering changes, update `MessageBubble.svelte` on both platforms
3. Compare diffs before copying because both files have already diverged

Desktop currently has an extra presentation-only streaming component,
`AgentActivityIndicator.svelte`, mounted from `MessageBubble.svelte` when an
assistant stream is active but no response text is streaming. It derives its
inline phase text from existing `message.steps`; it does not add SSE events or
backend state. On desktop, `tool_call_delta` also flushes any pending response
or thinking buffer before switching the activity phase to `Formulating...`, so
pre-tool preamble text is visible before the phase label is shown. Mobile has
not been replicated yet.

### Adding a new theme

1. Add it to `themes.ts` in both apps
2. Update `SettingsPanel.svelte` in both apps independently

### Adding a new theme color token

1. Add the token to `ThemeColors`
2. Add the matching CSS custom property to `themeColorCssVariables`
3. Add a value for every theme in desktop and mobile
4. Run `npm run check` in both apps

### Adding a new settings field

1. Add the type to `types/index.ts` in both apps
2. Add the API call to `services/api.svelte.ts` in both apps
3. Add UI to `SettingsPanel.svelte` with platform-appropriate presentation

### Adding a new account-related endpoint or component

1. Backend first — extend `Nymeria/nymeria/triggers/api.py` with the right `Depends(require_admin_user)` or `Depends(verify_api_key)`. Use the same `HTTPException(detail=...)` shape as the existing endpoints so the frontend's `_toastAndExtractError` parser picks up the message.
2. Add the response type to `types/index.ts` in both apps (mirror Pydantic field names exactly).
3. Add the API method to `services/api.svelte.ts` in both apps using the existing `_toastAndExtractError` pattern — every account/admin endpoint must route 401/403/409 through it so the global toast layer stays consistent.
4. Build / extend the component under `components/account/` in both apps. Re-use `Avatar`, `RoleChip`, `Modal`, `Button` for visual consistency.
5. Update both apps' `components/account/index.ts` barrel.
6. Mobile-only divergences: skip `connectionsStore` (single-connection), prefer 40px touch targets, use bottom-sheet patterns over popovers.
7. Document — append the new component to the Account UI table above and to the component reference in [`frontend-accounts.md`](frontend-accounts.md).
4. Check for desktop-only tabs such as proxy/integration controls before mirroring UI structure

### Adding a new chat feature (e.g., reactions, editing)

1. Types: `types/index.ts` on both platforms
2. API: `services/api.svelte.ts` on both platforms
3. Store logic: `stores/chat.svelte.ts` on both platforms
4. Message rendering: `MessageBubble.svelte` on both platforms, checking existing divergence first
5. Input UI: `InputBar.svelte` independently, because interaction models differ substantially

### Adding thread list features (folders, sorting, pins)

The `threadsStore` already supports all features in both. Only the `ThreadList.svelte` UI differs:
- Desktop: Full-featured (1258 lines)
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
