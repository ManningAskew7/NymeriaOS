import { threadsStore } from './threads.svelte';
import { chatStore } from './chat.svelte';
import { autonomousStore } from './autonomous.svelte';
import { startSyncPoll } from './syncPoll.svelte';
import { api, hasActiveStreamForThread } from '$lib/services/api.svelte';

export type SwitchResult =
  | { success: true }
  | { success: false; error: string };

function isNotFoundError(error: unknown): boolean {
  return error instanceof Error && /\b404\b/.test(error.message);
}

/**
 * Shared thread-switch pipeline. Both ThreadList and RightPanel call this.
 *
 * @param threadId - Thread to switch to
 * @param options.ensureTitle - If provided, calls ensureThread(id, title) first
 *                              (for navigating to backend-only threads not yet in sidebar)
 */
export async function switchToThread(
  threadId: string,
  options?: { ensureTitle?: string }
): Promise<SwitchResult> {
  if (threadId === threadsStore.currentThreadId) return { success: true };

  if (options?.ensureTitle) {
    threadsStore.ensureThread(threadId, options.ensureTitle);
  }
  threadsStore.selectThread(threadId);
  chatStore.prepareForThreadSwitch();
  chatStore.setLoadingHistory(true);

  try {
    const [history, stats, status] = await Promise.all([
      api.getThreadHistory(threadId),
      api.getThreadContextStats(threadId),
      api.getThreadStatus(threadId),
    ]);

    // Stale navigation guard — user clicked another thread during the await.
    // Leave isLoadingHistory alone: whichever thread the user ended up on
    // owns the flag now (set by its own switchToThread call).
    if (threadsStore.currentThreadId !== threadId) return { success: true };

    chatStore.setMessages(history.messages);
    chatStore.setContextStats(stats);
    chatStore.setActiveModel(stats?.model ?? null);
    chatStore.setLoadingHistory(false);

    // Start cross-client sync poller
    startSyncPoll(threadId, status);

    // Stream recovery — resume if thread has an active task or interactive stream
    const hasAutonomousTask = autonomousStore.hasActiveTask(threadId);
    const hasInteractiveStream = hasActiveStreamForThread(threadId);

    if (hasAutonomousTask || hasInteractiveStream) {
      const lastMsg = chatStore.messages[chatStore.messages.length - 1];
      if (lastMsg?.role !== 'assistant') {
        chatStore.addAssistantMessage();
      } else {
        chatStore.setLastMessageStreaming();
      }
      chatStore.setStreaming(true);
      if (hasAutonomousTask) {
        autonomousStore.resumeStreamingForThread(threadId);
      }
    }

    return { success: true };
  } catch (error) {
    console.error('Failed to load thread history:', error);
    // Don't report failure if user already navigated away — the error is stale
    if (threadsStore.currentThreadId !== threadId) return { success: true };
    chatStore.clearMessages();
    chatStore.setLoadingHistory(false);
    if (isNotFoundError(error)) {
      threadsStore.clearCurrent();
      await threadsStore.syncFromBackend();
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    return { success: false, error: message };
  }
}
