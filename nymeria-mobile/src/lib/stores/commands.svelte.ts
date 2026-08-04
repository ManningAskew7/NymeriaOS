/**
 * Commands Store
 *
 * The one client-side cache for the backend slash-command catalog
 * (GET /commands). Serves both consumers that used to fetch independently:
 * the composer palette (reactive `commands`) and the send-path routing
 * (awaitable `chatStreamRoots()`). One promise-memoized fetch per session,
 * shared by concurrent callers; the catalog is actor-scoped, so an account
 * switch resets the cache.
 *
 * Failure is quiet by design: the palette stays closed and routing degrades
 * to the static fallback set below. A failed fetch is retried at most once
 * per RETRY_AFTER_MS, so a backend restart recovers the palette without the
 * old request-per-slash-send storm (and without the deleted per-component
 * caches' opposite extreme, a fallback pinned for the whole session).
 */

import { api } from '$lib/services/api.svelte';
import { registerIdentityReloadHook } from './config.svelte';
import type { SlashCommandInfo } from '$lib/types';

const RETRY_AFTER_MS = 15_000;

// How long the send path waits on an in-flight catalog fetch before routing
// with the fallback set. listCommands carries no abort/timeout, so a HUNG
// (not refused) backend would otherwise stall the send indefinitely (review
// 2026-08-04); the fetch keeps running and lands for the next consumer.
const ROOTS_AWAIT_TIMEOUT_MS = 3_000;

// Slash commands whose execution_kind is `chat_stream` on the backend must
// route through the /chat SSE endpoint, not /commands/execute (the command
// service rejects them with "handled outside the command service"). The live
// set is derived from the catalog so new registrations cannot drift (the old
// hardcoded list was missing /done and /resume, which broke both); this
// static fallback covers a failed catalog fetch.
const CHAT_STREAM_FALLBACK_ROOTS: ReadonlySet<string> = new Set([
  '/compact', '/orchestrate', '/goal', '/skill', '/kit', '/quick', '/done', '/resume'
]);

export function createCommandsStore() {
  let commands = $state<SlashCommandInfo[]>([]);
  let loaded = $state(false);
  let catalogPromise: Promise<void> | null = null;
  let lastFailureAt: number | null = null;
  let identityGeneration = 0;

  // Reset on account switch / sign-out: the catalog is actor-scoped.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    commands = [];
    loaded = false;
    catalogPromise = null;
    lastFailureAt = null;
  });

  /**
   * Fetch the catalog once per session (memoized promise; concurrent callers
   * share the in-flight request). No-op inside the retry window after a
   * failure. Callers can fire-and-forget (`void ensureLoaded()`) or await it
   * before reading `chatStreamRoots()`.
   */
  function ensureLoaded(): Promise<void> {
    if (catalogPromise) return catalogPromise;
    if (lastFailureAt !== null && Date.now() - lastFailureAt < RETRY_AFTER_MS) {
      return Promise.resolve();
    }
    const requestGeneration = identityGeneration;
    catalogPromise = api.listCommands().then(
      (list) => {
        if (requestGeneration !== identityGeneration) return;
        commands = list;
        loaded = true;
        lastFailureAt = null;
      },
      (e) => {
        if (requestGeneration !== identityGeneration) return;
        console.warn('Failed to load the slash-command catalog:', e);
        lastFailureAt = Date.now();
        catalogPromise = null;
      }
    );
    return catalogPromise;
  }

  /**
   * The chat-stream roots for the send-path routing fork. Never blocks a
   * send longer than ROOTS_AWAIT_TIMEOUT_MS: past the deadline it derives
   * from whatever is cached (the static fallback when nothing is).
   */
  async function chatStreamRoots(): Promise<ReadonlySet<string>> {
    await Promise.race([
      ensureLoaded(),
      new Promise<void>((resolve) => setTimeout(resolve, ROOTS_AWAIT_TIMEOUT_MS)),
    ]);
    const roots = new Set<string>();
    for (const cmd of commands) {
      if (cmd.execution_kind === 'chat_stream' && cmd.path.length > 0) {
        roots.add(`/${cmd.path[0]}`);
      }
    }
    return roots.size > 0 ? roots : CHAT_STREAM_FALLBACK_ROOTS;
  }

  return {
    get commands() { return commands; },
    get loaded() { return loaded; },
    ensureLoaded,
    chatStreamRoots,
  };
}

export const commandsStore = createCommandsStore();
