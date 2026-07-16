import { threadsStore } from './threads.svelte';
import { chatStore } from './chat.svelte';
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

    // Stream recovery — resume if the thread has an in-flight turn. The
    // cases bind differently and must not be conflated: an interactive turn
    // this client started streams via chatStore alone (MainPanel's
    // chatStream loop); any other live holder turn (another client's turn,
    // an autonomous turn, or this client's own turn surviving a dropped
    // stream) is watched through the turn buffer attach path (the single
    // transcript renderer since backlog #90 slice 3). An unattachable turn
    // (truncated buffer, pre-restart turn, older backend without the status
    // `turn` block) settles from history at task end / the next sync poll.
    const hasInteractiveStream = hasActiveStreamForThread(threadId);

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
    } else if (status?.turn?.state === 'live' && !status.turn.truncated) {
      // Watch the live holder turn (backlog #87; autonomous turns since
      // #90 slice 2): MainPanel consumes the request, trims the hydrated
      // turn-so-far at the anchor, and replays + tails the turn buffer.
      // A truncated buffer cannot replay, so skip the attach instead of
      // stalling on an unattachable turn.
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
