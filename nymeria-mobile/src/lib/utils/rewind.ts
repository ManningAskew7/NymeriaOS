// Rewind targeting + orchestration for the edit/rewind bubble affordances
// (backlog #12). The backend endpoint addresses a rewind by the LangGraph id
// of the target user message (to_message_id, exact) with trailing-exchange
// counting (steps) as CLI-era fallback. Hydrated bubbles carry that id as
// message.graphMessageId; bubbles appended live during this session only have
// a client-generated id, so they are mapped onto a fresh authoritative
// history fetch by ordinal position instead.
//
// Mobile has no sync-poll baseline (unlike desktop), so reconciliation after a
// rewind is a plain history refetch via chatStore.setMessages.

import type { Message } from '$lib/types';
import { api } from '$lib/services/api.svelte';
import { chatStore } from '$lib/stores/chat.svelte';

/**
 * A message counts toward ordinal rewind mapping when it is a real user
 * prompt: user role and not an autonomous wake-up. Autonomous prompts can be
 * absent from the local transcript (display toggle off) while still present
 * in the authoritative fetch, so both sides must skip them for the ordinals
 * to line up. Hidden-but-present user messages (memory seeds render as
 * system entries; regex-hidden prompts stay user role in BOTH lists) are
 * consistent between the two lists and need no special casing.
 */
export function isCountableUserMessage(message: Message): boolean {
  return message.role === 'user' && !message.autonomousSource;
}

/**
 * Number of visible transcript messages a rewind at `targetIndex` discards
 * beyond the target itself. Display copy only; the backend decides the real
 * removal from graph state.
 */
export function computeVisibleBlastRadius(messages: Message[], targetIndex: number): number {
  if (targetIndex < 0 || targetIndex >= messages.length) return 0;
  return messages.length - targetIndex - 1;
}

/**
 * Resolve the backend graph message id for a transcript bubble.
 *
 * Fast path: hydrated bubbles carry graphMessageId. Fallback for bubbles
 * appended live this session: fetch the authoritative history (autonomous
 * prompts included so nothing is missing server-side), count how many
 * countable user prompts sit at/after the target locally (k), and take the
 * k-th countable user prompt from the end of the authoritative list.
 * Returns null when the bubble cannot be resolved (e.g. compacted away
 * between render and tap); callers should refresh and surface an error.
 */
export async function resolveRewindTarget(
  threadId: string,
  localMessages: Message[],
  targetId: string
): Promise<string | null> {
  const targetIndex = localMessages.findIndex((m) => m.id === targetId);
  if (targetIndex < 0) return null;
  const target = localMessages[targetIndex];
  if (target.graphMessageId) return target.graphMessageId;
  if (!isCountableUserMessage(target)) return null;

  const k = localMessages.slice(targetIndex).filter(isCountableUserMessage).length;
  const fresh = await api.getThreadHistory(threadId, { showAutonomousPrompts: true });
  let seen = 0;
  for (let i = fresh.messages.length - 1; i >= 0; i--) {
    const candidate = fresh.messages[i];
    if (!isCountableUserMessage(candidate)) continue;
    seen++;
    if (seen === k) {
      // The ordinal map assumes the local and authoritative tails agree. They
      // can diverge (a failed send leaving a local-only bubble, or an external
      // message not yet reconciled in), which would silently shift the cut
      // point. Only trust the match when the authoritative message actually
      // contains the target bubble's text; the stored copy may carry
      // server-side prefixes (time context), so containment rather than
      // equality. A false negative falls back to the stale_target reload,
      // after which bubbles are hydrated with graph ids and the fast path
      // applies.
      const localText = target.content.trim();
      const freshText = (candidate.content ?? '').trim();
      if (localText && !freshText.includes(localText)) return null;
      return candidate.graphMessageId ?? null;
    }
  }
  return null;
}

export interface RewindOutcome {
  ok: boolean;
  /** Backend response when ok. */
  removed?: number;
  /** Set when !ok: 'stale_target' | 'busy' | 'error', for caller copy. */
  reason?: 'stale_target' | 'busy' | 'error';
  /** Raw error for humanizeError when reason is 'busy' or 'error'. */
  error?: unknown;
}

/**
 * Rewind the thread so `targetId` (a transcript bubble id) and everything
 * after it are removed, then reconcile local state. On success the local
 * transcript is truncated. On a stale target (404) the transcript is
 * reloaded from the backend so the UI stops showing messages that no longer
 * exist.
 */
export async function rewindToMessage(threadId: string, targetId: string): Promise<RewindOutcome> {
  let graphId: string | null = null;
  try {
    graphId = await resolveRewindTarget(threadId, chatStore.messages, targetId);
  } catch (e) {
    return { ok: false, reason: 'error', error: e };
  }
  if (!graphId) {
    await reloadHistoryAfterRewindMismatch(threadId);
    return { ok: false, reason: 'stale_target' };
  }

  try {
    const result = await api.rewindThread(threadId, { toMessageId: graphId });
    chatStore.truncateFromMessage(targetId);
    return { ok: true, removed: result.removed };
  } catch (e) {
    const text = e instanceof Error ? e.message : String(e);
    if (text.includes('Rewind target not found')) {
      await reloadHistoryAfterRewindMismatch(threadId);
      return { ok: false, reason: 'stale_target' };
    }
    if (text.includes('Thread is busy')) {
      return { ok: false, reason: 'busy', error: e };
    }
    return { ok: false, reason: 'error', error: e };
  }
}

/** Reload the transcript when local state and graph state disagree. */
async function reloadHistoryAfterRewindMismatch(threadId: string): Promise<void> {
  try {
    const history = await api.getThreadHistory(threadId);
    chatStore.setMessages(history.messages);
  } catch {
    // Leave the transcript as-is; a later refresh reconciles.
  }
}
