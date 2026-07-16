/**
 * Cross-client sync poller.
 *
 * Polls the current thread's lightweight checkpoint status every few seconds
 * to detect changes from other clients (Outlook, browser, desktop app).
 * Fetches full history/context only when the checkpoint revision changes,
 * processing finishes, or no revision baseline is known.
 *
 * Skips polling while the current client is actively streaming (it already
 * has the latest data from its own SSE connection).
 */

import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { api } from '$lib/services/api.svelte';
import { debugLog } from '$lib/utils/debug';
import type { ThreadStatus } from '$lib/types';

const SYNC_POLL_INTERVAL = 5000; // 5 seconds

let pollTimer: ReturnType<typeof setInterval> | null = null;
let baselineKnown = false;
let lastKnownRevision: string | null = null;
let wasProcessing = false;
let pollInFlight = false;

export function startSyncPoll(threadId: string, initialStatus?: ThreadStatus) {
  stopSyncPoll();
  baselineKnown = initialStatus !== undefined;
  lastKnownRevision = initialStatus?.revision ?? null;
  wasProcessing = initialStatus?.processing ?? false;

  pollTimer = setInterval(() => {
    void pollThreadStatus(threadId);
  }, SYNC_POLL_INTERVAL);
}

export function stopSyncPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  pollInFlight = false;
}

export function updateThreadStatusBaseline(status: ThreadStatus) {
  baselineKnown = true;
  lastKnownRevision = status.revision;
  wasProcessing = status.processing;
}

/**
 * Refresh only the lightweight status baseline after this client streamed a
 * turn locally. This prevents the next idle poll from re-fetching history the
 * SSE stream already applied to the UI.
 */
export async function refreshThreadSyncBaseline(threadId: string) {
  try {
    const status = await api.getThreadStatus(threadId);
    if (threadsStore.currentThreadId !== threadId) return;
    updateThreadStatusBaseline(status);
  } catch {
    // Keep the previous baseline; the next normal poll can recover.
  }
}

async function pollThreadStatus(threadId: string) {
  if (pollInFlight) return;

  if (threadsStore.currentThreadId !== threadId) {
    stopSyncPoll();
    return;
  }

  // Skip if actively streaming — our own SSE has the latest.
  if (chatStore.isStreaming) return;

  pollInFlight = true;
  try {
    const status = await api.getThreadStatus(threadId);
    if (threadsStore.currentThreadId !== threadId || chatStore.isStreaming) return;

    // Live-attach (backlog #87): a holder turn started elsewhere while this
    // thread was already open (no thread switch, so navigation's attach
    // branch never saw it). Hand off to the viewer attach instead of the
    // history-refresh below; the attach flow owns rendering from here.
    // Streaming self-suppression above keeps this off this client's own
    // turns (including turns already being watched via attach, which hold
    // isStreaming). isLoadingHistory is unset here by construction (the poll
    // only starts after switchToThread's history load completes). A
    // truncated buffer cannot replay: attaching to it would park the panel
    // on "Reconnecting" (same gate as navigation), so skip and let the
    // normal refresh below reconcile from history instead.
    if (status.turn?.state === 'live' && !status.turn.truncated) {
      chatStore.requestViewerAttach(threadId, status.turn);
      updateThreadStatusBaseline(status);
      return;
    }

    const revisionChanged = baselineKnown && status.revision !== lastKnownRevision;
    const processingJustFinished = wasProcessing && !status.processing;
    const needsBaseline = !baselineKnown;

    if (needsBaseline || revisionChanged || processingJustFinished) {
      if (needsBaseline) {
        debugLog('[Sync] Establishing revision baseline, refreshing history');
      } else if (revisionChanged) {
        debugLog(`[Sync] Revision changed (${lastKnownRevision ?? 'null'} -> ${status.revision ?? 'null'}), refreshing`);
      } else {
        debugLog('[Sync] Processing finished, final refresh');
      }

      const [history, stats] = await Promise.all([
        api.getThreadHistory(threadId),
        api.getThreadContextStats(threadId),
      ]);
      if (threadsStore.currentThreadId !== threadId || chatStore.isStreaming) return;

      chatStore.setMessages(history.messages);
      chatStore.setContextStats(stats);
      chatStore.setActiveModel(stats?.model ?? null);
    }

    updateThreadStatusBaseline(status);
  } catch {
    // Polling is best-effort; explicit navigation and SSE reconnect paths
    // still refresh persisted state.
  } finally {
    pollInFlight = false;
  }
}
