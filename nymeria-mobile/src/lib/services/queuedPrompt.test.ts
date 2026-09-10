import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('$lib/services/api.svelte', () => ({
  abortCurrentStream: vi.fn(),
  api: { stopThread: vi.fn() }
}));
import { api as stopApi, abortCurrentStream } from '$lib/services/api.svelte';
import { createChatStore } from '$lib/stores/chat.svelte';
import { finishPromotedTurn, queuedPromptEvents } from './queuedPrompt';
import type { QueuedBatch, SSEEvent, SSEEventType, StopThreadResult } from '$lib/types';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
function event(type: SSEEventType, data: unknown = {}): SSEEvent {
  return { type, data, timestamp: new Date() };
}
function setup(onPromoted?: (id: string, controller: AbortController) => boolean) {
  const store = createChatStore();
  store.addUserMessage('original');
  store.addAssistantMessage();
  const original = store.beginStream('thread');
  let next = deferred<SSEEvent | null>();
  let controller!: AbortController;
  const api = {
    async *queuePromptStream(_message: string, _thread: string, ctrl: AbortController) {
      controller = ctrl;
      ctrl.signal.addEventListener('abort', () => next.resolve(null));
      while (!ctrl.signal.aborted) {
        const value = await next.promise;
        next = deferred<SSEEvent | null>();
        if (!value) return;
        yield value;
      }
    },
    withdrawQueuedPrompt: vi.fn(async (_thread: string, _id: string) => {})
  };
  let promoted: number | undefined;
  const output: SSEEvent[] = [];
  const run = (async () => {
    for await (const value of queuedPromptEvents('follow-up', 'thread', store, api,
      onPromoted ?? ((id) => {
        promoted = store.promotePendingPrompt(id, 'thread');
        return promoted !== undefined;
      }))) output.push(value);
  })();
  return { store, original, api, run, output,
    get id() { return store.pendingPrompts[0].id; },
    get controller() { return controller; },
    get promoted() { return promoted; },
    send: (value: SSEEvent | null) => next.resolve(value) };
}
const batch: QueuedBatch = { id: 'batch', total: 2, inputs: [
  { promptId: 'p1', position: 1, source: 'user', sourceLabel: 'Manning', userId: 'owner', enqueuedAt: 1, text: 'first', modelContent: '[Trigger: user | source: Manning]\n\nfirst' },
  { promptId: 'p2', position: 2, source: 'callable', sourceLabel: 'Research Agent', userId: 'owner', enqueuedAt: 2, text: 'second', modelContent: '[Trigger: callable | source: Research Agent]\n\nsecond' }
] };

beforeEach(() => vi.clearAllMocks());

describe('queued prompt admission and withdrawal', () => {
  it.each([false, true])('remembers early X until receipt, including navigation (%s)', async (navigate) => {
    const flow = setup();
    await flow.store.requestPendingPromptWithdrawal(flow.id);
    expect(flow.store.pendingPrompts[0].status).toBe('withdrawing');
    expect(flow.controller.signal.aborted).toBe(false);
    expect(flow.api.withdrawQueuedPrompt).not.toHaveBeenCalled();
    if (navigate) flow.store.prepareForThreadSwitch();
    expect(flow.controller.signal.aborted).toBe(false);
    flow.send(event('prompt_queued', { promptId: 'server-id', position: 1 }));
    await vi.waitFor(() => expect(flow.api.withdrawQueuedPrompt).toHaveBeenCalledExactlyOnceWith('thread', 'server-id'));
    await flow.run;
    expect(flow.store.pendingPrompts).toEqual([]);
    expect(flow.output).toEqual([]);
    expect(flow.store.messages.map(m => m.content)).not.toContain('follow-up');
    expect(stopApi.stopThread).not.toHaveBeenCalled();
  });

  it('renders a promoted turn despite early X and leaves its controller alive during output', async () => {
    const flow = setup();
    await flow.store.requestPendingPromptWithdrawal(flow.id);
    flow.send(event('turn_started', { turnId: 'promoted' }));
    await vi.waitFor(() => expect(flow.output.map(e => e.type)).toEqual(['turn_started']));
    expect(flow.store.pendingPrompts).toEqual([]);
    expect(flow.store.messages.map(m => m.content)).toEqual(['original', '', 'follow-up', '']);
    expect(flow.store.messages.at(-1)?.status).toBe('streaming');
    expect(flow.store.isStreaming).toBe(true);
    expect(flow.store.finishStream(flow.original)).toBe(false);
    expect(flow.store.isStreaming).toBe(true);
    expect(flow.controller.signal.aborted).toBe(false);
    flow.send(event('response', { content: 'new answer' }));
    await vi.waitFor(() => expect(flow.output.at(-1)?.data).toEqual({ content: 'new answer' }));
    flow.send(event('done'));
    await vi.waitFor(() => expect(flow.output.at(-1)?.type).toBe('done'));
    flow.send(null);
    await flow.run;
    expect(flow.api.withdrawQueuedPrompt).not.toHaveBeenCalled();
  });

  it('detaches off-thread promotion without rendering into the new thread', async () => {
    const flow = setup(() => false);
    flow.store.beginStream('other-thread');
    flow.store.setMessages([]);
    flow.send(event('turn_started', { turnId: 'offscreen' }));
    await flow.run;
    expect(flow.store.messages).toEqual([]);
    expect(flow.output).toEqual([]);
    expect(flow.api.withdrawQueuedPrompt).not.toHaveBeenCalled();
  });

  it('withdraws from the actual queue thread when dispatch changes the destination', async () => {
    const flow = setup();
    flow.send(event('prompt_queued', { promptId: 'target-prompt', queueThreadId: 'dispatch-target', position: 1 }));
    await vi.waitFor(() => expect(flow.store.pendingPrompts[0].promptId).toBe('target-prompt'));
    await flow.store.requestPendingPromptWithdrawal(flow.id);
    await flow.run;
    expect(flow.api.withdrawQueuedPrompt).toHaveBeenCalledExactlyOnceWith('dispatch-target', 'target-prompt');
    expect(flow.store.pendingPrompts).toEqual([]);
  });

  it('keeps failed withdrawal honest and retryable after the observer disconnects', async () => {
    const flow = setup();
    flow.send(event('prompt_queued', { promptId: 'server-id', position: 1 }));
    await vi.waitFor(() => expect(flow.store.pendingPrompts[0].promptId).toBe('server-id'));
    const id = flow.id;
    flow.api.withdrawQueuedPrompt.mockRejectedValueOnce(new Error('offline'));
    await flow.store.requestPendingPromptWithdrawal(id);
    expect(flow.store.pendingPrompts[0]).toMatchObject({ status: 'withdraw_error', errorMessage: expect.stringContaining('may still run') });
    expect(flow.controller.signal.aborted).toBe(false);
    flow.send(null);
    await flow.run;
    await flow.store.requestPendingPromptWithdrawal(id);
    expect(flow.api.withdrawQueuedPrompt).toHaveBeenCalledTimes(2);
    expect(flow.store.pendingPrompts).toEqual([]);
    expect(stopApi.stopThread).not.toHaveBeenCalled();
  });

  it('reports a lost receipt as uncertain and lets X dismiss only its notice', async () => {
    const flow = setup();
    flow.send(null);
    await flow.run;
    expect(flow.store.pendingPrompts[0]).toMatchObject({ status: 'error', errorMessage: expect.stringContaining('may still run') });
    await flow.store.requestPendingPromptWithdrawal(flow.id);
    expect(flow.store.pendingPrompts).toEqual([]);
    expect(flow.api.withdrawQueuedPrompt).not.toHaveBeenCalled();
  });

  it.each(['withdrawn', 'restored'])('settles a %s receipt without rendering a reply', async code => {
    const flow = setup();
    flow.send(event('error', { code, message: 'settled' }));
    await flow.run;
    expect(flow.store.pendingPrompts).toEqual([]);
    expect(flow.output).toEqual([]);
  });
});

describe('queue identity and visible batch ownership', () => {
  it('consumes exact IDs and catches receipts that arrive after injection', () => {
    const store = createChatStore();
    const foreign = store.addPendingPrompt('still promotable');
    const late = store.addPendingPrompt('receipt late');
    const local = store.addPendingPrompt('known');
    const ctrl = new AbortController();
    store.registerPendingPromptAbort(foreign, ctrl);
    store.setPendingPromptReceipt(local, 'p1', 1);
    store.addQueuedBatch(batch, ['p1', 'p2']);
    expect(store.pendingPrompts.map(p => p.id)).toEqual([foreign, late]);
    expect(ctrl.signal.aborted).toBe(false);
    store.setPendingPromptReceipt(late, 'p2', 2);
    expect(store.pendingPrompts.map(p => p.id)).toEqual([foreign]);
    store.removePendingPromptByServerId('unrelated');
    expect(store.pendingPrompts.map(p => p.id)).toEqual([foreign]);
    store.setPendingPromptReceipt(foreign, 'external-withdrawal', 1);
    store.removePendingPromptByServerId('external-withdrawal');
    expect(store.pendingPrompts).toEqual([]);
    expect(ctrl.signal.aborted).toBe(true);
  });

  it('renders one expanded-source batch and rebuilds it exactly once on full-turn replay', () => {
    const store = createChatStore();
    store.addUserMessage('original');
    store.addAssistantMessage();
    const generation = store.beginStream('thread');
    store.addResponseStep('before batch');
    store.addQueuedBatch(batch);
    store.addQueuedBatch(batch); // Duplicate wire delivery.
    store.addResponseStep('shared reply');
    store.flushStreamingBuffers();
    expect(store.messages.map(m => m.kind ?? m.role)).toEqual(['user', 'assistant', 'queued_batch', 'assistant']);
    expect(store.messages[2].queuedBatch).toEqual(batch);
    expect(store.messages[2].content).toBe(batch.inputs.map(i => i.modelContent).join('\n\n'));
    expect(store.messages[3].steps).toEqual([{ type: 'response', content: 'shared reply' }]);
    store.resetStreamForReplay(generation);
    expect(store.messages.map(m => m.content)).toEqual(['original', '']);
    store.addQueuedBatch(batch);
    expect(store.messages.filter(m => m.kind === 'queued_batch')).toHaveLength(1);
    store.beginStream('new-thread');
    store.resetStreamForReplay(generation);
    expect(store.messages.filter(m => m.kind === 'queued_batch')).toHaveLength(1);
  });

  it('a late Stop result restores its returned text without clearing or aborting a successor', async () => {
    const flow = setup();
    const stop = deferred<StopThreadResult>();
    vi.mocked(stopApi.stopThread).mockReturnValueOnce(stop.promise);
    const stopping = flow.store.stopGenerating('thread');
    flow.send(event('turn_started', { turnId: 'promoted' }));
    await vi.waitFor(() => expect(flow.promoted).toBeDefined());
    const queued = flow.store.addPendingPrompt('successor queued input');
    stop.resolve({ status: 'idle', holder: null, heldSeconds: 0,
      restoredPrompts: [{ text: 'old restored', sourceLabel: 'User', userId: 'owner', enqueuedAt: 1 }] });
    await stopping;
    expect(flow.store.isStreaming).toBe(true);
    expect(flow.store.pendingPrompts.map(p => p.id)).toEqual([queued]);
    expect(flow.store.composerRestore).toBe('old restored');
    expect(abortCurrentStream).not.toHaveBeenCalled();
    flow.send(event('done'));
    await vi.waitFor(() => expect(flow.output.at(-1)?.type).toBe('done'));
    flow.send(null);
    await flow.run;
    flow.store.clearPendingPrompts();
  });
});


describe('promoted turn settlement used by both panels', () => {
  it.each([false, true])('retains dispatched output and its link after prefix reconciliation, including gap errors (%s)', async hasError => {
    const flow = setup();
    flow.send(event('turn_started', { turnId: 'target' }));
    await vi.waitFor(() => expect(flow.promoted).toBeDefined());
    const userId = flow.store.messages.at(-2)!.id;
    flow.store.setLastMessageContent('inline target answer');
    flow.store.setMessages(flow.store.messages.map((m, i) => i === flow.store.messages.length - 1
      ? { ...m, dispatchInfo: { threadId: 'target', title: 'Target' } } : m));
    if (hasError) flow.store.setLastMessageError('Open the linked thread to view its saved history.');
    const projectedReply = structuredClone(flow.store.messages.at(-1));
    const canonical = [{ ...flow.store.messages[0], content: 'original' },
      { ...flow.store.messages[1], content: 'full predecessor answer', status: 'complete' as const }];
    await finishPromotedTurn(flow.store, {
      getThreadHistory: async () => ({ messages: canonical, threadId: 'thread' }),
      getThreadContextStats: async () => null
    }, 'thread', flow.promoted!, userId, () => flow.store.ownsStream(flow.promoted!), true, hasError);
    expect(flow.store.messages.map(m => m.content)).toEqual(['original', 'full predecessor answer', 'follow-up', projectedReply!.content]);
    expect(flow.store.messages.at(-1)?.dispatchInfo).toEqual({ threadId: 'target', title: 'Target' });
    expect(flow.store.messages.at(-1)?.status).toBe(hasError ? 'error' : 'complete');
    expect(flow.store.isStreaming).toBe(false);
    flow.send(event('done'));
    await vi.waitFor(() => expect(flow.output.at(-1)?.type).toBe('done'));
    flow.send(null);
    await flow.run;
  });

  it('does not replace a successor while predecessor history is in flight', async () => {
    const store = createChatStore();
    store.addAssistantMessage();
    const first = store.beginStream('thread');
    const history = deferred<import('$lib/types').ThreadHistory>();
    const settling = finishPromotedTurn(store, {
      getThreadHistory: () => history.promise, getThreadContextStats: async () => null
    }, 'thread', first, 'unused', () => store.ownsStream(first), false, false);
    store.addUserMessage('newer');
    store.addAssistantMessage();
    store.beginStream('thread');
    history.resolve({ threadId: 'thread', messages: [] });
    await settling;
    expect(store.messages.map(m => m.content)).toEqual(['', 'newer', '']);
    expect(store.isStreaming).toBe(true);
  });

  it('a first-frame gap promotes and hands recovery the holder identity', async () => {
    const flow = setup();
    flow.send(event('turn_replay_gap', { turnId: 'holder' }));
    await vi.waitFor(() => expect(flow.promoted).toBeDefined());
    expect(flow.store.pendingPrompts).toEqual([]);
    expect(flow.output).toEqual([expect.objectContaining({ type: 'turn_replay_gap', data: { turnId: 'holder' } })]);
    flow.send(event('done'));
    await vi.waitFor(() => expect(flow.output.at(-1)?.type).toBe('done'));
    flow.send(null);
    await flow.run;
  });
});


it.each([false, true])('rewinds the identified batch without removing the original turn or restoring autonomous text (%s)', autonomous => {
  const store = createChatStore();
  store.addUserMessage('original request');
  store.addAssistantMessage();
  store.beginStream('thread');
  store.addResponseStep('original answer');
  const identified = { ...batch, inputs: batch.inputs.map((input, i) => ({ ...input, messageId: `graph-${i}` })) };
  store.addQueuedBatch(identified);
  store.handleTurnRewound({ toMessageId: 'graph-0', prompt: 'first\n\nsecond', content: 'Batch refused', autonomous });
  expect(store.messages.map(m => m.kind ?? m.role)).toEqual(['user', 'assistant', 'turn_rewound']);
  expect(store.messages[0].content).toBe('original request');
  expect(store.messages[1].steps).toEqual([{ type: 'response', content: 'original answer' }]);
  expect(store.composerRestore).toBe('first');
  expect(store.isStreaming).toBe(false);
});
