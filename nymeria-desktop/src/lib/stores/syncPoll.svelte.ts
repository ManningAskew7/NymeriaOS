/**
 * Cross-client sync poller.
 *
 * Polls the current thread's history every few seconds to detect changes
 * from other clients (Outlook, browser, desktop app). Refreshes when:
 *  - Message count changes (new message added)
 *  - Thread is still processing (assistant message being built)
 *
 * Skips polling while the current client is actively streaming (it already
 * has the latest data from its own SSE connection).
 */

import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { api } from '$lib/services/api.svelte';

const SYNC_POLL_INTERVAL = 5000; // 5 seconds

let pollTimer: ReturnType<typeof setInterval> | null = null;
let lastKnownMessageCount = 0;
let wasProcessing = false;

export function startSyncPoll(threadId: string, initialMessageCount?: number) {
  stopSyncPoll();
  lastKnownMessageCount = initialMessageCount ?? chatStore.messages.length;
  wasProcessing = false;

  pollTimer = setInterval(() => {
    // Stop if thread changed
    if (threadsStore.currentThreadId !== threadId) {
      stopSyncPoll();
      return;
    }
    // Skip if actively streaming — our own SSE has the latest
    if (chatStore.isStreaming) return;

    // Fetch both history and context stats in parallel
    Promise.all([
      api.getThreadHistory(threadId),
      api.getThreadContextStats(threadId),
    ]).then(([history, stats]) => {
      if (threadsStore.currentThreadId !== threadId || chatStore.isStreaming) return;

      const messageCountChanged = history.messages.length !== lastKnownMessageCount;
      const isProcessing = !!(stats as Record<string, unknown>)?.processing;
      const processingJustFinished = wasProcessing && !isProcessing;

      // Refresh if: new messages, thread is processing, or processing just finished
      if (messageCountChanged || isProcessing || processingJustFinished) {
        if (messageCountChanged) {
          console.log(`[Sync] Messages changed (${lastKnownMessageCount} → ${history.messages.length}), refreshing`);
        } else if (isProcessing) {
          console.log('[Sync] Thread still processing, refreshing');
        } else {
          console.log('[Sync] Processing just finished, final refresh');
        }
        lastKnownMessageCount = history.messages.length;
        chatStore.setMessages(history.messages);
        chatStore.setContextStats(stats);
        chatStore.setActiveModel(stats?.model ?? null);
      }

      wasProcessing = isProcessing;
    }).catch(() => {});
  }, SYNC_POLL_INTERVAL);
}

export function stopSyncPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** Update the baseline so the next poll doesn't spuriously refresh. */
export function updateMessageCount(count: number) {
  lastKnownMessageCount = count;
}
