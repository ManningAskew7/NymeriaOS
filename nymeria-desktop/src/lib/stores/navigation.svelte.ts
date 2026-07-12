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

    // Stream recovery — resume if the thread has an in-flight turn. The two
    // cases bind differently and must not be conflated: an interactive turn
    // streams via chatStore alone (MainPanel's chatStream loop), while an
    // autonomous turn renders through the autonomous store, which needs to bind
    // its own activeMessagesByThread entry and replay the buffered turn so far.
    const hasInteractiveStream = hasActiveStreamForThread(threadId);
    const hasAutonomousTask = autonomousStore.hasActiveTask(threadId);

    if (hasInteractiveStream) {
      // Only reuse the last assistant message when it represents the in-flight
      // turn (status === 'streaming'). History always hydrates messages as
      // 'complete', so a completed assistant at the tail is the *previous*
      // turn's reply — the incoming stream is a new turn and needs a fresh
      // placeholder. Without this, handoff-triggered streams overwrite the
      // prior reply (tool calls graft onto it, then response steps hide the
      // old content).
      const lastMsg = chatStore.messages[chatStore.messages.length - 1];
      if (lastMsg?.role === 'assistant' && lastMsg.status === 'streaming') {
        chatStore.setLastMessageStreaming();
      } else {
        chatStore.addAssistantMessage();
      }
      chatStore.setStreaming(true);
    } else if (hasAutonomousTask) {
      // Bind a streaming message and replay the turn so far (applies the same
      // graft-safe reuse rule internally), then live events render.
      autonomousStore.attachToThread(threadId);
    } else if (status?.turn?.state === 'live') {
      // A holder turn is running that this client did not start (another
      // client of the same user, or this client's own turn surviving a
      // dropped stream): watch it live (backlog #87). MainPanel consumes
      // the request, trims the hydrated turn-so-far, and replays + tails
      // the turn buffer.
      chatStore.requestViewerAttach(threadId, status.turn);
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
    const message = error instanceof Error ? error.message : 'Could not open that thread.';
    return { success: false, error: message };
  }
}
