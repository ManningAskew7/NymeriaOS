# Frontend Accounts UI — Architecture & Debugging Guide

This is the developer reference for everything the desktop and mobile apps render around **multi-user identity**: the bottom-bar avatar, the account menu, the saved-account switcher, the add-account modal, the Account / Users settings tabs, the copy-once token dialog, and the global error toast layer that surfaces 401/403/409 from the account-related endpoints.

It does **not** cover backend semantics — those live in [`accounts.md`](accounts.md) (data model, bootstrap, service token) and [`api.md`](api.md) (HTTP endpoint reference). Read those first if you're touching the server. Read this if you're touching the client.

---

## TL;DR — What's where

```
nymeria-desktop/src/lib/components/account/   ← all account UI (mobile mirrors)
├── Avatar.svelte                deterministic-colour initial circle
├── RoleChip.svelte              `admin` / `user` pill
├── AccountBadge.svelte          sidebar bottom-bar trigger (replaces ConnectionSwitcher)
├── AccountMenu.svelte           popover/sheet — Manage account / Switch / Add / Sign out
├── AccountSwitcher.svelte       saved-account list with cached identity (desktop only)
├── AddAccountSheet.svelte       Modal: URL + token → /me preview → Save (desktop only)
├── AccountTab.svelte            SettingsPanel "Account" tab (self management)
├── UsersTab.svelte              SettingsPanel "Users" tab (admin only)
├── TokenManagementSection.svelte    list + issue + revoke (+ rotate-all in admin mode)
├── PlatformLinkingSection.svelte    Discord/Telegram/Twitch ID list + add/remove
├── CopyOnceTokenDialog.svelte   raw-token-shown-ONCE warning + copy
├── avatar.ts                    pure helpers (initials, hash → colour, name fallbacks)
└── index.ts                     barrel export

nymeria-desktop/src/lib/stores/
├── config.svelte.ts             identity, signOut(), updateIdentityDisplayName()
├── connections.svelte.ts        saved entries with cached identity (desktop only)
└── errors.svelte.ts             toast queue + pushAuthInvalid()

nymeria-desktop/src/lib/services/api.svelte.ts
└── _toastAndExtractError()      single chokepoint for /me/* and /admin/* failures
```

Mobile lives at `nymeria-mobile/src/lib/...` with the same names. Two desktop-only files have no mobile equivalent because mobile is single-connection: `AccountSwitcher.svelte` and `AddAccountSheet.svelte`.

---

## The user-facing surfaces

Three places the user can interact with their account:

### 1. Sidebar bottom-bar (`AccountBadge`)

`Sidebar.svelte:110` (desktop) renders `AccountBadge`. On mobile, `LeftPanel.svelte` renders it as a 40×40 button alongside Settings and Notifications. Shows:

- Deterministic-colour avatar (initials hashed from `user_id`).
- Display name (or email fallback) with a `RoleChip`.
- A subtle border indicator for **disabled** (red) or **unverified** (grey) states.

Click → opens `AccountMenu`.

### 2. Account menu (`AccountMenu`)

Popover on desktop (positioned absolutely above the badge — same pattern as `NotificationCenter`), bottom sheet on mobile. Contains:

| Item | Action |
|---|---|
| Header card | Avatar + name + email + role chip + server hostname |
| Manage account | Opens SettingsPanel → Account tab |
| Manage users | (admin only) Opens SettingsPanel → Users tab |
| Switch account | (desktop) Opens `AccountSwitcher`. (mobile) Opens Connection settings |
| Add account | (desktop) Opens `AddAccountSheet`. (mobile) Opens Connection settings |
| Sign out | Calls `connectionsStore.clearActive()` (desktop) + `configStore.signOut()` |

State is lifted up to `AccountBadge` so the badge can coordinate menu / switcher / add-sheet visibility.

### 3. SettingsPanel tabs

Two new tabs registered in `components/common/SettingsPanel.svelte`:

- **Account** — always visible. Renders `AccountTab.svelte`.
- **Users** — visible only when `configStore.identity?.role === 'admin'`. Renders `UsersTab.svelte`.

`AccountTab` sections:
- Identity (avatar, email read-only, editable display name → `PATCH /me`, role chip, account ID)
- API tokens (`<TokenManagementSection mode="self" />`)
- Linked platforms (admin only — `<PlatformLinkingSection userId={me.id} />`)
- Sign out (destructive button)

`UsersTab` is master-detail: a search-filtered list of all users (`GET /admin/users`), and clicking a row swaps the table for a detail view of that user with editable name/role, enable/disable toggle, admin-mode `TokenManagementSection`, `PlatformLinkingSection`, and a delete button.

---

## Data flow

### Identity resolution (boot + every account switch)

```
User launches app
  ↓
configStore loads from localStorage (apiUrl, apiKey, last-known identity)
  ↓
routes/+page.svelte onMount calls configStore.refreshIdentity()
  ↓
GET /me (Bearer apiKey)
  ↓
Updates configStore.identity AND module-level `currentIdentityId`
  (used by scopedKey() to namespace localStorage as `nymeria-{user_id}-*`)
  ↓
If user_id changed → migrateLegacyKeys() runs once + identity reload hooks fire
  ↓
Per-feature stores (threads, todos, etc.) re-read from their now-scoped keys
  ↓
AccountBadge renders the resolved identity reactively
```

### AccountSwitcher (desktop)

Each saved entry (`SavedConnection` in `types/index.ts`) has a cached `identity?` field plus `identityCheckedAt` and `identityError`. On boot the active entry is auto-verified (`connectionsStore.verifyEntry(id)` runs in a 500ms-deferred `setTimeout` at the bottom of `connections.svelte.ts`). Each row in the switcher shows the cached identity; the per-row "⋯ → Re-verify" menu re-hits `/me` against that entry's URL+token.

When the user clicks a different account:
1. `connectionsStore.switchTo(id)` — disconnects SSE/polling, clears chat, sets new apiUrl/apiKey, reloads threads
2. `configStore.refreshIdentity()` — fetches `/me` for the new account
3. `connectionsStore.verifyEntry(id)` — refreshes the cached identity for the just-activated entry

### Token issuance flow

```
TokenManagementSection: user clicks "Issue token"
  ↓
Modal opens asking for optional label
  ↓
api.issueMyToken(label) — POST /me/tokens { label }
  (or api.issueUserToken(userId, label) in admin mode)
  ↓
Returns { raw_token, metadata: TokenInfo }
  ↓
showCopyDialog = true; raw_token bound to CopyOnceTokenDialog.rawToken
  ↓
User clicks Copy → navigator.clipboard.writeText(raw_token)
  ↓
User clicks "I've saved it" → dialog closes, raw_token state cleared
  ↓
load() re-fetches the list to show the new entry
```

The raw token only ever lives in the issuing component's local state. It is not persisted anywhere — closing the dialog drops it forever. This matches the backend contract (`accounts.md::Tokens` — only sha256 is stored).

### Error toast wiring

`api.svelte.ts::_toastAndExtractError()` is a single chokepoint for the 16 new account/admin endpoints. Every non-2xx response goes through it before the throw:

| Status | Toast kind | Side effect |
|---|---|---|
| 401 | `auth_invalid` | `errorsStore.pushAuthInvalid()` → `connectionsStore.clearActive()` (desktop) + `configStore.signOut()` |
| 403 | `forbidden_admin` | (none) |
| 409 + body mentions "last admin" / "only enabled admin" | `last_admin` | (none) |
| 409 + body mentions "thread" / "todo" / "owns" | `resource_owned` | (none) |
| Any other non-2xx | `generic` | (none) |

The store (`stores/errors.svelte.ts`) holds an in-memory queue. `ErrorToast.svelte` (mounted at root in `routes/+page.svelte`) reads `errorsStore.queue` reactively and renders a stack of severity-tinted cards. Auto-dismiss is 5s for most kinds; `auth_invalid` is persistent (`ttlMs: 0`) since the user must take action.

To push a toast from anywhere:
```ts
import { errorsStore } from '$lib/stores/errors.svelte';
errorsStore.push({ kind: 'generic', message: 'Something specific went wrong' });
```

---

## Component reference

### `Avatar.svelte`

| Prop | Type | Notes |
|---|---|---|
| `identity` | `AccountIdentity \| null \| undefined` | Source of initials + colour seed |
| `size` | `number` (default 32) | Pixel diameter |
| `state` | `'connected' \| 'disabled' \| 'unverified' \| 'loading' \| 'plain'` | Border treatment |

Helpers in `avatar.ts`:
- `avatarInitials(identity)` — first+last initial of `display_name`, falls back to first 2 chars of email local-part, then `?`.
- `avatarBackground(id)` — `hsl(<hue> 55% 42%)` over an 8-hue palette indexed by `hashString(id) % 8`.
- `identityDisplayName(identity)` / `identitySecondary(identity)` — display string helpers used by every account surface for consistency.

### `RoleChip.svelte`

Tiny uppercase pill. Two variants: `admin` (warm `--warning` accent) and `user` (muted neutral). Sizes: `sm` (default) and `xs` (used inside dense rows like `AccountSwitcher`).

### `AccountBadge.svelte`

Owns the `showMenu` / `showSwitcher` / `showAddAccount` state and renders `AccountMenu`, `AccountSwitcher`, and `AddAccountSheet` as siblings inside `.account-wrapper` so absolute-positioned popovers anchor correctly. Two render modes (full vs collapsed-icon) mirror the existing `.footer-btn` / `.icon-btn` pattern in `Sidebar.svelte`.

### `AccountMenu.svelte`

Reads `configStore.identity` and `configStore.apiUrl` reactively. Click-outside / Escape close handlers are bound on mount and torn down on unmount via `$effect`. `Manage users` item is gated on `identity?.role === 'admin'`.

### `AccountSwitcher.svelte` (desktop only)

Reads `connectionsStore.connections` reactively. Per-row state (which row's `⋯` menu is open, which row is being inline-renamed) is local. The `handleSwitch()` flow waits for both `configStore.refreshIdentity()` and `connectionsStore.verifyEntry(id)` to complete before closing — guarantees the badge shows the new account immediately.

### `AddAccountSheet.svelte` (desktop only)

Wraps `Modal`. Local state: URL + token + optional label. `Test connection` runs `/health` then `/me` (raw `fetch`, not `api.svelte` — needs to use the form's URL/token, not the global config). On success, shows a preview card with the resolved identity. `Save` calls `connectionsStore.add()` then `connectionsStore.verifyEntry()` then `connectionsStore.switchTo()` (when `switchAfterSave` is true).

### `AccountTab.svelte`

Pure composition — orchestrates Identity card + Token section + Platforms section + Sign-out section. Inline display-name editor calls `configStore.updateIdentityDisplayName()` which wraps `PATCH /me` and refreshes the cached identity in one go.

### `UsersTab.svelte`

Two views in one component, switched by `selectedId !== null`:
- **List view** — search input + filtered user table + `+ New user` button.
- **Detail view** — back button + identity card + Profile section (editable name/role + enable-toggle) + Tokens (`<TokenManagementSection mode="admin" userId={selected.id} />`) + Platforms (`<PlatformLinkingSection userId={selected.id} />`) + Delete section.

`detailDirty` tracks whether the Save-changes button should light up. The Delete button is force-disabled when `selected.id === configStore.identity?.id` ("can't delete yourself"). When the backend returns 409 because the target still owns threads or todos, the toast layer surfaces the message via the `resource_owned` kind.

### `TokenManagementSection.svelte`

Polymorphic via `mode: 'self' | 'admin'`:
| Mode | List | Issue | Revoke | Rotate-all |
|---|---|---|---|---|
| `self` | `GET /me/tokens` | `POST /me/tokens` | `DELETE /me/tokens/{prefix}` | (hidden) |
| `admin` | `GET /admin/users/{userId}/tokens` | `POST /admin/users/{userId}/tokens` | `DELETE /admin/users/{userId}/tokens/{prefix}` | `POST /admin/users/{userId}/tokens/rotate` |

`load()` is wrapped in `untrack()` inside the `$effect` so its internal `if (loading) return` re-entry guard doesn't establish `loading` as a tracked dependency of the effect (which would race itself on every fetch toggle). Issue button's disabled state is tied to `issuing`, **not** `loading` — list-load delays must never block issuing.

### `PlatformLinkingSection.svelte`

Always uses the admin endpoints (`GET / POST / DELETE /admin/users/{userId}/platforms`). For self use in `AccountTab` this still works because the section is only rendered for admins viewing themselves. The provider dropdown is hardcoded to `discord | telegram | twitch` matching backend `PlatformIdentityResponse`. Provider ID input is monospace because these are platform-native numeric IDs.

### `CopyOnceTokenDialog.svelte`

Modal-wrapped warning + monospace token block + Copy button. Resets its `copied` and `copyError` state every time it re-opens (`$effect` on `isOpen`). The optional `forUser` prop changes the wording from "use this token in another browser…" to "issued for X — hand it off securely…" for admin-issued tokens.

---

## Routing through the API service

All account/admin methods live in `services/api.svelte.ts`. They share the same shape:

```ts
async someEndpoint(args): Promise<Result> {
  const response = await fetch(`${this.getBaseUrl()}/...`, {
    method: '...',
    headers: this.getHeaders(),
    body: JSON.stringify(...)
  });
  if (!response.ok) {
    throw new Error(await this._toastAndExtractError(response, 'Failed to X'));
  }
  return response.json();
}
```

`_toastAndExtractError(response, fallback)`:
1. Reads JSON body and extracts `detail` (FastAPI's default error key).
2. Pushes the appropriate toast based on status + body hints.
3. Returns the human-readable message string for the caller to throw.

If you add a new account/admin endpoint, follow this exact pattern so error toasts stay consistent. If you need a 409 handled with a new kind, extend `_toastAndExtractError`'s body-substring check (the matchers are intentionally loose; the backend is the source of truth for the message text).

---

## Common debugging scenarios

| Symptom | First file to check | Why |
|---|---|---|
| Badge doesn't show identity | `routes/+page.svelte` — does `configStore.refreshIdentity()` run on mount? `config.svelte.ts::refreshIdentity` for the actual fetch. | Identity drives everything; if `/me` isn't returning a value the badge falls back to "loading" / "Not signed in". |
| Badge shows "Account disabled" or red ring | `connections.svelte.ts::verifyEntry` and `config.svelte.ts::refreshIdentity` — both set `identityError` / clear identity on 401. | The `disabled` state usually comes from a 401 with the disabled-account body marker. |
| "Issue token" button is greyed out | `TokenManagementSection.svelte` — `disabled={issuing}` only; if it's stuck, check `issuing` state. The fix from commit `10a7697` ensures `loading` no longer disables it. | Historical bug — `disabled={loading}` + `$effect` racing was the original problem. |
| AccountSwitcher rows show "Tap to verify" | `connections.svelte.ts::verifyEntry` failed for that entry — either the URL is unreachable or the token is bad. Check `connection.identityError`. | The auto-verify on boot only runs for the active entry; others stay unverified until the user opens the switcher and triggers per-row Re-verify. |
| Copy-once dialog never appears after issue | `TokenManagementSection.svelte::handleIssue` — verify `showCopyDialog = true` runs after the `await api.issueMyToken()` and that `issuedRawToken` is populated. | Both must be set before the modal renders. |
| 401 on every account endpoint after backend restart | `routes/+page.svelte` mounts `ErrorToast`; the new admin/me methods auto-trigger `pushAuthInvalid` on 401. The user should land on SetupWizard with a "Session expired" toast. | If they don't, `_toastAndExtractError` may not have been invoked — check the endpoint follows the pattern. |
| 403 from `/admin/users/...` | Caller is not admin. `UsersTab` is gated on `isAdmin` so this shouldn't happen from inside the app, but a stale localStorage with a demoted user could trigger it. | Demote-yourself isn't possible (last-admin guard) so this is rare; usually means the token belongs to a non-admin and the UI was loaded from cache. |
| 409 deleting a user | Backend refuses if target owns threads / todos. Toast says "Cannot delete". User must reassign or delete those resources first. | Ownership counts are visible in the UsersTab side panel (`thread_count`, `todo_count` on `AdminUser`). |
| 409 on PATCH role | Last-admin guard. Demoting / disabling the only enabled admin returns 409 with "last admin" or "only enabled admin" in the body. The toast surfaces as `last_admin` kind. | Add another admin first. |
| Linked platforms section invisible for non-admin self user | Intentional — both GET and POST `/admin/users/{id}/platforms` are admin-only. The section in `AccountTab.svelte` is wrapped in `{#if isAdmin}`. | If self-platform-list ever needs to work for non-admins, the backend would need a `/me/platforms` endpoint. |
| Toast layer doesn't appear | `routes/+page.svelte` mounts `<ErrorToast />` after `</AppShell>` (desktop) / `</MobileShell>` (mobile). If you removed it, none of the auto-toast wiring works. | Also check `errorsStore.queue` in DevTools — if the queue has entries but nothing renders, the component itself is broken. |
| Per-user data leaks across accounts on switch | `config.svelte.ts::scopedKey` and `migrateLegacyKeys` — first switch migrates legacy unscoped keys to scoped ones. Per-feature stores must register identity-reload hooks via `registerIdentityReloadHook` to re-read on switch. | The `connectionsStore.switchTo` also calls `threadsStore.reset()` + `syncFromBackend()` for an immediate reload; check that path. |

---

## Adding a new account-related feature

If you're adding something like "user activity log" or "two-factor enrollment", follow this template:

1. **Backend first**: add the endpoint in `Nymeria/nymeria/triggers/api.py` with the right `Depends(require_admin_user)` or `Depends(verify_api_key)`. Wrap the `AccountsRepo` method, raise `HTTPException` with the same `detail` shape as existing endpoints (so `_toastAndExtractError`'s parser keeps working).
2. **Add types**: append the new `Pydantic` response shape to `nymeria-{desktop,mobile}/src/lib/types/index.ts` (mirror the field names exactly).
3. **Add API method**: in both apps' `api.svelte.ts`, add a method that follows the existing `_toastAndExtractError` pattern.
4. **Build the component**: under `components/account/`. Re-use `Avatar`, `RoleChip`, `Modal`, `Button`, and the section-styling conventions in `AccountTab.svelte` / `UsersTab.svelte` for consistency.
5. **Wire into a tab**: `SettingsPanel.svelte` is where Account / Users tabs are registered. New tabs go at the end of the tab list (desktop) or in the array (mobile).
6. **Update `index.ts` barrel** in both apps' `components/account/`.
7. **Document**: append to this file's component reference + add to `desktop-vs-mobile.md` if it diverges between apps.
8. **Verify**: `npm run check` + `npx vite build` in both apps. Smoke against the backend to confirm types match the response shape.

---

## Testing flow

Cross-reference [`chrome-mcp-testing.md`](chrome-mcp-testing.md) for general frontend testing setup. Account-specific manual flow:

1. **Fresh setup** — wipe localStorage (`localStorage.clear()`), launch app. SetupWizard should render.
2. **Step 3 of wizard** — paste bootstrap token, click Test. Should show "You'll be signed in as owner@localhost [admin]" preview before Continue.
3. **Identity surface** — sidebar bottom shows your avatar + name + role chip. Click → AccountMenu lists Manage account + Manage users (admin only) + Switch + Add + Sign out.
4. **Issue + use a token** — Settings → Account → Issue token → label it → copy from dialog. Open another browser, go to SetupWizard, paste URL + the new token. Should sign in as the same user.
5. **Revoke that token** — back in original browser, click trash on that token row. The other browser's next request returns 401 → "Session expired" toast → routed to SetupWizard.
6. **Create a non-admin user** — Settings → Users → + New user → email + role=user. Copy the issued token. Add a new account in your switcher with that token.
7. **Switch to non-admin** — Account menu → Switch account → click the new entry. Toast confirms switch. Verify the Users tab is hidden, MCP install button shows "Admin only", and `self_modify` tool has the admin-only badge.
8. **Try to demote `default`** — switch back to admin, Settings → Users → default → role=user → Save. Should toast "Cannot demote/disable the only enabled admin" with the `last_admin` kind.
9. **Delete an empty user** — create a user, immediately delete from the detail view. Should succeed.
10. **Delete a user with threads** — chat as a non-admin user, switch back to admin, try to delete that user. Should toast "Cannot delete: still owns threads/todos".
11. **Mobile parity** — repeat 3, 4, 5, 6 on mobile. Note that mobile has no AccountSwitcher / AddAccountSheet — switching is via Settings → Connection.

---

## Where this fits in the overall code map

- Account UI consumes [`accounts.md`](accounts.md)'s data model and [`api.md`](api.md)'s HTTP surface.
- Identity scoping of localStorage keys (the `nymeria-{user_id}-*` prefix) is set up in `config.svelte.ts::scopedKey` and `registerIdentityReloadHook` — every per-feature store goes through this.
- The Setup Wizard's identity preview lives in `components/common/SetupWizard.svelte` and reuses `Avatar` + `RoleChip` from `account/`.
- The `+page.svelte` root mounts `<ErrorToast />` so it sits above every modal — don't put it inside a panel that conditionally renders.
