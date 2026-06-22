# Per-thread backend connections (design)

Status: Proposed. Not implemented. This captures what the desktop and mobile
clients would need so a single client can hold threads that live on different
backends at the same time (for example one thread on a personal backend and
another on a work backend).

Code is authoritative when this doc and the implementation disagree. File
references are accurate as of 2026-05-26 and may move.

## Motivation

Today a client talks to exactly one backend at a time. The goal is per-thread
backends: thread A can be live on backend X while thread B is live on backend Y,
with one unified sidebar. This is a deliberate architecture change, because the
current clients are built on a single global "active backend" assumption.

## Current architecture (the single-backend assumption)

The whole client points at one backend through `configStore`:

- `configStore.apiUrl` / `configStore.apiKey` are the one live backend
  (`nymeria-desktop/src/lib/stores/config.svelte.ts`).
- The API client is a global singleton. `ApiBase.getBaseUrl()` returns
  `configStore.apiUrl` and `getHeaders()` returns `configStore.apiKey`
  (`src/lib/services/api/base.ts`); `resolveUserId()` reads
  `configStore.identity`. Every store and component imports the shared `api`
  instance from `src/lib/services/api.svelte.ts`.
- Identity is global: one `currentIdentityId`, and localStorage is namespaced by
  that single id via `scopedKey()` in `config.svelte.ts`. The thread cache key is
  `nymeria-threads-<user_id>` (`src/lib/stores/threads.svelte.ts`). The backend
  URL is not part of the key, so two backends that resolve to the same user id
  share one cached thread list.
- Realtime is single-stream: `autonomousStore` opens one SSE connection to one
  `/autonomous/stream?user_id=...` (`src/lib/stores/autonomous.svelte.ts`).
  `notificationStore`, `syncPoll`, and health polling are likewise single-backend.
- `connectionsStore` stores saved connections with one active at a time
  (`activeConnectionId`); `switchTo()` / `applyConnection()` tear down and
  re-point the whole app (`src/lib/stores/connections.svelte.ts`).
- The `Thread` type has no backend or connection field
  (`src/lib/types/index.ts`).

Because the target backend is read from one global place on every call, there is
currently no way to send two concurrent requests to two different backends from
the same client.

## Required changes

### 1. Associate each thread with a connection

Add a `connectionId` to `Thread` (persisted in the thread cache). Each sidebar
row then knows which backend it belongs to. The sidebar becomes a union of
threads from every connected backend rather than one backend's list.

### 2. Make the API client connection-scoped

Replace the global singleton plus `getBaseUrl()` / `getHeaders()` that read
`configStore` with a factory, for example `apiFor(connection)`, that binds a
client to one connection's url, token, and identity. Every thread-scoped call
must first resolve the thread's `connectionId` and use that client: chat send,
`getThreadHistory`, `getThreadStatus`, `/context`, todos, config, attachments,
`stop`, metadata PATCH, delete. This is the bulk of the work, because the shared
`api` instance is referenced throughout the stores and components.

### 3. Per-connection identity and cache scoping

`config.svelte.ts` holds a single `currentIdentityId` and scopes localStorage by
it. With multiple backends, each connection has its own identity. The
`connectionsStore` already caches a per-connection `identity` on each
`SavedConnection` via `verifyEntry()`, which is a useful starting point. The
thread cache key must become per connection (for example
`nymeria-threads-<connectionId>`), and the sidebar merges the per-connection
lists. This is the same "scope the cache by backend" idea that is optional today
but becomes mandatory here.

### 4. Per-connection realtime

`autonomousStore` must manage one SSE stream per connected backend (each stream
carries its own user id) and route each event to the thread on the matching
connection. The same fan-out applies to `notificationStore`, `syncPoll`, and
health polling: one instance per connection, or a multiplexer keyed by
connection.

### 5. Redefine "active connection"

`activeConnectionId`, `switchTo()`, and `applyConnection()` stop meaning "the one
backend the app uses" and become "the default backend for new chats." The global
teardown plus reset plus resync in `applyConnection()` becomes a per-connection
refresh rather than a full wipe.

## Hard constraint: cross-backend agent features

Agent-to-agent features assume the target thread lives on the same backend,
which resolves them server side and has no knowledge of other backends:

- callable threads (`nymeria/agents/tool_factory.py`),
- cross-thread `@mention` dispatch,
- `spawn_thread`,
- handoffs.

If thread A on backend X tries to call thread B on backend Y, nothing bridges
that today. Per-thread backends must either restrict these features to
same-backend threads, or add a client or relay layer that brokers cross-backend
calls. Decide this early, because it shapes the thread-to-connection data model.

## Prerequisites already in place

- The legacy thread-metadata migration (`migrate_from_frontend` and
  `POST /threads/metadata/migrate`) has been removed. It pushed one backend's
  cached thread titles onto whatever backend was connected, which created empty
  "ghost" threads and is fundamentally incompatible with multiple backends. The
  frontend now always treats the backend as authoritative.
- Thread creation is already lazy: `threadsStore.createThread()` only creates the
  thread locally; the backend claims ownership on the first real write via
  `require_thread_access` (see `nymeria/triggers/api.py`). Opening a tab does not
  create a backend thread.
- Switching the active connection already clears and resyncs the sidebar through
  `connectionsStore.applyConnection()`, so a switch never shows another backend's
  cached titles.

## Suggested rollout

1. Add `Thread.connectionId` and the `apiFor(connection)` factory; route
   thread-scoped calls through it while keeping one default connection.
2. Move identity and cache scoping to per connection (this subsumes the
   "cache by backend" hardening, so it is not done twice).
3. Multiplex SSE, notifications, sync polling, and health per connection.
4. Make the sidebar a union across connections and add per-thread connection
   affordances in the UI.
5. Decide and implement the cross-backend story for callable threads, mentions,
   spawn_thread, and handoffs.

## Open questions

- Should a thread be movable between backends, or is `connectionId` fixed at
  creation?
- How should folders, teams, and pins behave across backends (per connection, or
  unified client-side)?
- How are credential prompts and per-thread tool bindings surfaced when several
  backends are live at once?
