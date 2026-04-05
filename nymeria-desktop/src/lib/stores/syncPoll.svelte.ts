/**
 * Cross-client sync poller.
 *
 * Polls the current thread's history every few seconds to detect messages
 * from other clients (Outlook, browser, desktop app). Only refreshes when
 * the message count changes, keeping overhead minimal.
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

export function startSyncPoll(threadId: string, initialMessageCount?: number) {
  stopSyncPoll();
  lastKnownMessageCount = initialMessageCount ?? chatStore.messages.length;

  pollTimer = setInterval(() => {
    // Stop if thread changed
    if (threadsStore.currentThreadId !== threadId) {
      stopSyncPoll();
      return;
    }
    // Skip if actively streaming — our own SSE has the latest
    if (chatStore.isStreaming) return;

    api.getThreadHistory(threadId).then((history) => {
      if (threadsStore.currentThreadId !== threadId || chatStore.isStreaming) return;
      if (history.messages.length !== lastKnownMessageCount) {
        console.log(`[Sync] Messages changed (${lastKnownMessageCount} → ${history.messages.length}), refreshing`);
        lastKnownMessageCount = history.messages.length;
        chatStore.setMessages(history.messages);
        api.getThreadContextStats(threadId).then((stats) => {
          if (threadsStore.currentThreadId === threadId) {
            chatStore.setContextStats(stats);
            chatStore.setActiveModel(stats?.model ?? null);
          }
        }).catch(() => {});
      }
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
