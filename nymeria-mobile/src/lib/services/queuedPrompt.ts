import type { ChatApi } from './api/chat';
import type { ThreadsApi } from './api/threads';
import type { createChatStore } from '$lib/stores/chat.svelte';
import type { SSEEvent } from '$lib/types';

type QueueApi = Pick<ChatApi, 'queuePromptStream' | 'withdrawQueuedPrompt'>;
type QueueStore = ReturnType<typeof createChatStore>;

/** Own one admission request until it is queued, withdrawn, or becomes a turn. */
export async function* queuedPromptEvents(
  message: string,
  threadId: string,
  store: QueueStore,
  api: QueueApi,
  onPromoted: (localId: string, controller: AbortController) => boolean
): AsyncGenerator<SSEEvent> {
  const localId = store.addPendingPrompt(message);
  const controller = new AbortController();
  let promptId: string | undefined;
  let queueThreadId = threadId;
  const preamble: SSEEvent[] = [];
  let withdrawalRequested = false;
  let withdrawal: Promise<void> | undefined;
  let promoted = false;
  let settled = false;

  const withdraw = (): Promise<void> => {
    if (promoted || settled) return Promise.resolve();
    withdrawalRequested = true;
    store.setPendingPromptStatus(localId, 'withdrawing');
    if (!promptId) return Promise.resolve(); // Keep the receipt observer alive.
    if (withdrawal) return withdrawal;
    const serverId = promptId;
    withdrawal = Promise.resolve().then(async () => {
      try {
        await api.withdrawQueuedPrompt(queueThreadId, serverId);
        settled = true;
        store.removePendingPrompt(localId);
      } catch {
        if (!settled) {
          withdrawalRequested = false;
          store.setPendingPromptStatus(localId, 'withdraw_error',
            'Could not withdraw. This prompt may still run. Retry withdrawal.');
        }
      } finally {
        withdrawal = undefined;
      }
    });
    return withdrawal;
  };

  store.registerPendingPromptAbort(localId, controller);
  store.registerPendingPromptWithdrawal(localId, withdraw);
  try {
    for await (const event of api.queuePromptStream(message, threadId, controller)) {
      if (!promoted && event.type === 'dispatched') { preamble.push(event); continue; }
      if ((event.type === 'turn_started' || event.type === 'turn_replay_gap') && !promoted) {
        promoted = onPromoted(localId, controller);
        if (!promoted) { settled = true; return; }
        yield* preamble.splice(0);
      }
      if (promoted) {
        if (event.type === 'done' || event.type === 'error') settled = true;
        yield event;
        continue;
      }
      if (event.type === 'prompt_queued') {
        const data = event.data as { promptId?: string; position?: number; queueThreadId?: string };
        promptId = data.promptId;
        queueThreadId = data.queueThreadId || threadId;
        store.setPendingPromptReceipt(localId, promptId, data.position);
        if (withdrawalRequested) void withdraw();
      } else if (event.type === 'prompt_absorbed') {
        settled = true;
        store.removePendingPrompt(localId);
        return;
      } else if (event.type === 'error') {
        const data = event.data as { code?: string; message: string };
        settled = true;
        if (data.code === 'restored' || data.code === 'withdrawn') {
          store.removePendingPrompt(localId);
        } else {
          store.setPendingPromptStatus(localId, 'error', data.message);
        }
        return;
      }
    }
    if (!settled && !controller.signal.aborted) {
      throw new TypeError('The prompt connection ended before its outcome was received.');
    }
  } catch (error) {
    if (promoted) throw error; // The panel owns normal turn recovery.
    if (!controller.signal.aborted && !settled) {
      store.setPendingPromptStatus(localId, promptId ? 'withdraw_error' : 'error',
        promptId
          ? 'Lost the queue connection. This prompt may still run. You can withdraw it.'
          : 'Lost the receipt. This prompt may still run. Check the thread before retrying.');
    }
  } finally {
    // A known server ID remains withdrawable even after its observer disconnects.
    store.unregisterPendingPromptRequest(localId, !promoted && !settled && !!promptId);
    if (promoted || settled) controller.abort();
  }
}


/** Settle a promotion while retaining replies that belong to a dispatch target. */
export async function finishPromotedTurn(
  store: QueueStore,
  api: Pick<ThreadsApi, 'getThreadHistory' | 'getThreadContextStats'>,
  threadId: string,
  generation: number,
  userMessageId: string,
  ownsTurn: () => boolean,
  dispatched: boolean,
  hasError: boolean
): Promise<void> {
  try {
    if (hasError && !dispatched) return;
    const [history, stats] = await Promise.all([
      api.getThreadHistory(threadId), api.getThreadContextStats(threadId)
    ]);
    if (!ownsTurn()) return;
    if (dispatched) {
      // Dispatch output is absent from caller history. Replace only the prefix.
      const index = store.messages.findIndex(message => message.id === userMessageId);
      if (index < 0) return;
      store.flushStreamingBuffers();
      store.setMessages([...history.messages, ...store.messages.slice(index)]);
    } else {
      store.setMessages(history.messages);
    }
    store.setContextStats(stats);
    store.setActiveModel(stats?.model ?? null);
  } catch {
    if (ownsTurn() && !dispatched && !hasError) {
      store.setLastMessageError('Could not refresh saved history. Showing the received reply; reload this thread to check its final state.');
    }
  } finally {
    if (ownsTurn()) store.finishStream(generation);
  }
}
