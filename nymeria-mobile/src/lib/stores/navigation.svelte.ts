import { threadsStore } from './threads.svelte';
import { chatStore } from './chat.svelte';
import { api, hasActiveStreamForThread } from '$lib/services/api.svelte';
import { uiStore } from './ui.svelte';

export type SwitchResult =
  | { success: true }
  | { success: false; error: string };

function isNotFoundError(error: unknown): boolean {
  return error instanceof Error && /\b404\b/.test(error.message);
}

/**
 * Shared thread-switch pipeline for mobile.
 * Navigates to the chat panel after switching.
 */
export async function switchToThread(
  threadId: string,
  options?: { ensureTitle?: string }
): Promise<SwitchResult> {
  if (threadId === threadsStore.currentThreadId) {
    uiStore.goToChat();
    return { success: true };
  }

  if (options?.ensureTitle) {
    threadsStore.ensureThread(threadId, options.ensureTitle);
  }
  threadsStore.selectThread(threadId);
  chatStore.prepareForThreadSwitch();
  chatStore.setLoadingHistory(true);

  // Navigate to chat panel immediately
  uiStore.goToChat();

  try {
    const [history, stats, status] = await Promise.all([
      api.getThreadHistory(threadId),
      api.getThreadContextStats(threadId),
      // For the live-attach decision below (backlog #87); a status failure
      // must not block opening the thread.
      api.getThreadStatus(threadId).catch(() => null),
    ]);

    // Stale navigation guard. Leave isLoadingHistory alone: whichever
    // thread the user ended up on owns it now.
    if (threadsStore.currentThreadId !== threadId) return { success: true };

    chatStore.setMessages(history.messages);
    chatStore.setContextStats(stats);
    chatStore.setActiveModel(stats?.model ?? null);
    chatStore.setLoadingHistory(false);

    // Stream recovery for active interactive streams.
    // Only reuse the last assistant message when it is still streaming;
    // a 'complete' assistant message at the tail is the prior turn's reply
    // and must not absorb a new stream's tool calls / response.
    const hasInteractiveStream = hasActiveStreamForThread(threadId);
    if (hasInteractiveStream) {
      const lastMsg = chatStore.messages[chatStore.messages.length - 1];
      if (lastMsg?.role === 'assistant' && lastMsg.status === 'streaming') {
        chatStore.setLastMessageStreaming();
      } else {
        chatStore.addAssistantMessage();
      }
      chatStore.setStreaming(true);
    } else if (status?.turn?.state === 'live' && !status.turn.truncated) {
      // A holder turn is running that this client did not start (another
      // client of the same user, an autonomous turn since #90 slice 2, or
      // this client's own turn surviving a dropped stream): watch it live
      // (backlog #87). ChatPanel consumes the request, trims the hydrated
      // turn-so-far at the anchor (hidden-wakeup stubs included), and
      // replays + tails the turn buffer. A truncated buffer cannot replay,
      // so skip the attach instead of stalling on an unattachable turn; the
      // thread settles from history at task end (backlog #90 slice 3).
      chatStore.requestViewerAttach(threadId, status.turn);
    }

    return { success: true };
  } catch (error) {
    console.error('Failed to load thread history:', error);
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
