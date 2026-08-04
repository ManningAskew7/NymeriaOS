import { describe, expect, it, vi, beforeEach } from 'vitest';

// The chat store touches the api singleton (only in stopGenerating).
// Mock it so the heavy api/index.ts module chain isn't pulled in.
vi.mock('$lib/services/api.svelte', () => ({
  abortCurrentStream: vi.fn(),
  api: {
    stopThread: vi.fn().mockResolvedValue({
      status: 'idle',
      holder: null,
      heldSeconds: 0,
      restoredPrompts: [],
    }),
  },
}));

import { createChatStore } from './chat.svelte';
import { abortCurrentStream, api } from '$lib/services/api.svelte';
import type { Message, StopThreadResult } from '$lib/types';

function makeCompletedAssistant(content: string, id = 'prev-turn'): Message {
  return {
    id,
    role: 'assistant',
    content,
    timestamp: new Date('2026-05-17T12:00:00Z'),
    status: 'complete',
    steps: [{ type: 'response', content }],
    intermediateContent: undefined,
    toolCalls: [],
  };
}

function makeUser(content: string, id = 'user-1'): Message {
  return {
    id,
    role: 'user',
    content,
    timestamp: new Date('2026-05-17T12:00:01Z'),
    status: 'complete',
  };
}

describe('chatStore: edit-previous-prompt state (backlog #12)', () => {
  let store: ReturnType<typeof createChatStore>;

  function seedTranscript(): Message[] {
    const transcript = [
      makeUser('first prompt', 'u1'),
      makeCompletedAssistant('first reply', 'a1'),
      makeUser('second prompt', 'u2'),
      makeCompletedAssistant('second reply', 'a2'),
    ];
    store.setMessages(transcript);
    return transcript;
  }

  beforeEach(() => {
    store = createChatStore();
  });

  it('beginEdit/cancelEdit set and clear the edit fields', () => {
    seedTranscript();
    const image = {
      id: 'img-1',
      type: 'image' as const,
      dataUrl: 'data:image/png;base64,abc',
      mimeType: 'image/png',
      name: 'shot.png',
      size: 3,
    };

    store.beginEdit('u2', 'second prompt', [image]);
    expect(store.isEditing).toBe(true);
    expect(store.editingMessageId).toBe('u2');
    expect(store.editingDraft).toBe('second prompt');
    expect(store.editingImageAttachments).toEqual([image]);

    store.cancelEdit();
    expect(store.isEditing).toBe(false);
    expect(store.editingMessageId).toBeNull();
    expect(store.editingDraft).toBe('');
    expect(store.editingImageAttachments).toEqual([]);
  });

  it('truncateFromMessage drops the target and everything after it', () => {
    seedTranscript();
    store.truncateFromMessage('u2');
    expect(store.messages.map((m) => m.id)).toEqual(['u1', 'a1']);
  });

  it('truncateFromMessage is a no-op for unknown ids', () => {
    seedTranscript();
    store.truncateFromMessage('nope');
    expect(store.messages.map((m) => m.id)).toEqual(['u1', 'a1', 'u2', 'a2']);
  });

  it('truncating away the edited message clears edit state', () => {
    seedTranscript();
    store.beginEdit('u2', 'second prompt');
    store.truncateFromMessage('u2');
    expect(store.isEditing).toBe(false);
    expect(store.editingDraft).toBe('');
  });

  it('truncating after the edited message keeps edit state', () => {
    seedTranscript();
    store.beginEdit('u1', 'first prompt');
    store.truncateFromMessage('u2');
    expect(store.isEditing).toBe(true);
    expect(store.editingMessageId).toBe('u1');
  });

  it('prepareForThreadSwitch cancels an in-progress edit', () => {
    seedTranscript();
    store.beginEdit('u2', 'second prompt');
    store.prepareForThreadSwitch();
    expect(store.isEditing).toBe(false);
  });

  it('clearMessages cancels an in-progress edit (new-thread flow)', () => {
    seedTranscript();
    store.beginEdit('u2', 'second prompt');
    store.clearMessages();
    expect(store.isEditing).toBe(false);
    expect(store.editingDraft).toBe('');
  });

  it('handleTurnRewound drops the refused exchange, notices, and restores the prompt', () => {
    seedTranscript();
    store.handleTurnRewound({
      toMessageId: 'u2',
      prompt: 'second prompt',
      content: "Fable 5's classifier declined this turn.",
    });
    const ids = store.messages.map((m) => m.id);
    expect(ids.slice(0, 2)).toEqual(['u1', 'a1']);
    const notice = store.messages[store.messages.length - 1];
    expect(notice.role).toBe('system');
    expect(notice.kind).toBe('turn_rewound');
    expect(notice.content).toContain('declined');
    expect(store.consumeComposerRestore()).toBe('second prompt');
    expect(store.isStreaming).toBe(false);
  });

  it('handleTurnRewound on the first turn leaves only the notice', () => {
    store.setMessages([makeUser('poke it', 'u1')]);
    store.handleTurnRewound({ toMessageId: 'u1', prompt: 'poke it', content: 'Declined.' });
    expect(store.messages).toHaveLength(1);
    expect(store.messages[0].kind).toBe('turn_rewound');
  });

  it('handleTurnRewound ignores autonomous refusals (no surgery, no restore)', () => {
    seedTranscript();
    store.handleTurnRewound({
      toMessageId: 'u2',
      prompt: 'second prompt',
      content: 'Declined.',
      autonomous: true,
    });
    expect(store.messages.map((m) => m.id)).toEqual(['u1', 'a1', 'u2', 'a2']);
    expect(store.consumeComposerRestore()).toBe('');
  });

  it('handleTurnRewound no-ops when the anchor is gone and the tail is a settled turn', () => {
    store.setMessages([
      { ...makeUser('earlier', 'u1'), graphMessageId: 'g1' },
      makeCompletedAssistant('reply', 'a1'),
    ]);
    store.handleTurnRewound({
      toMessageId: 'gone-anchor',
      prompt: 'refused',
      content: 'Declined.',
    });
    expect(store.messages.map((m) => m.id)).toEqual(['u1', 'a1']);
    expect(store.consumeComposerRestore()).toBe('');
  });

  it('handleTurnRewound cuts precisely at the backend anchor (graphMessageId match)', () => {
    store.setMessages([
      { ...makeUser('first prompt', 'u1'), graphMessageId: 'g1' },
      makeCompletedAssistant('first reply', 'a1'),
      { ...makeUser('refused prompt', 'u2'), graphMessageId: 'g2' },
      makeCompletedAssistant('refused shell', 'a2'),
    ]);
    store.handleTurnRewound({
      toMessageId: 'g2',
      prompt: 'refused prompt',
      content: 'Declined.',
    });
    expect(store.messages.map((m) => m.id).slice(0, 2)).toEqual(['u1', 'a1']);
    expect(store.messages[store.messages.length - 1].kind).toBe('turn_rewound');
    expect(store.consumeComposerRestore()).toBe('refused prompt');
  });

  it('handleTurnRewound structural fallback cuts only the fresh optimistic send', () => {
    store.setMessages([
      { ...makeUser('earlier', 'u1'), graphMessageId: 'g1' },
      makeCompletedAssistant('reply', 'a1'),
      makeUser('refused live send', 'u2'),
    ]);
    store.handleTurnRewound({
      toMessageId: 'g-unknown-locally',
      prompt: 'refused live send',
      content: 'Declined.',
    });
    expect(store.messages.map((m) => m.id).slice(0, 2)).toEqual(['u1', 'a1']);
    expect(store.messages[store.messages.length - 1].kind).toBe('turn_rewound');
    expect(store.consumeComposerRestore()).toBe('refused live send');
  });

  it('handleTurnRewound structural fallback cuts a whole optimistic batch run', () => {
    store.setMessages([
      { ...makeUser('earlier', 'u1'), graphMessageId: 'g1' },
      makeCompletedAssistant('reply', 'a1'),
      makeUser('first queued', 'u2'),
      makeUser('second queued', 'u3'),
    ]);
    store.handleTurnRewound({
      toMessageId: 'g-unknown-locally',
      prompt: 'first queued\n\nsecond queued',
      content: 'Declined.',
    });
    expect(store.messages.map((m) => m.id).slice(0, 2)).toEqual(['u1', 'a1']);
    expect(store.messages).toHaveLength(3);
    expect(store.messages[2].kind).toBe('turn_rewound');
  });

  it('handleTurnRewound clears local pending prompts like other terminal handlers', () => {
    seedTranscript();
    store.addPendingPrompt('typed during the refused turn');
    store.handleTurnRewound({
      toMessageId: 'u2',
      prompt: 'second prompt',
      content: 'Declined.',
    });
    expect(store.pendingPrompts).toEqual([]);
  });
});

describe('chatStore: stop lifecycle (backlog #11 + #16)', () => {
  let store: ReturnType<typeof createChatStore>;

  function stopResult(overrides: Partial<StopThreadResult> = {}): StopThreadResult {
    return {
      status: 'stopping',
      holder: 'chat',
      heldSeconds: 2,
      restoredPrompts: [],
      ...overrides,
    };
  }

  function seedStreamingTurn() {
    store.setMessages([makeUser('go')]);
    store.addAssistantMessage();
    store.setStreaming(true);
    store.addToolCallStep('tc-1', 'slow_tool', {});
  }

  beforeEach(() => {
    store = createChatStore();
    vi.mocked(api.stopThread).mockReset();
    vi.mocked(abortCurrentStream).mockReset();
  });

  it('stopping response defers finalize until the cancelled frame', async () => {
    vi.mocked(api.stopThread).mockResolvedValue(stopResult());
    seedStreamingTurn();

    await store.stopGenerating('t1');

    // No optimistic edit: still streaming, marked stopping, stream not aborted.
    expect(store.isStopping).toBe(true);
    expect(store.isStreaming).toBe(true);
    expect(abortCurrentStream).not.toHaveBeenCalled();
    const last = store.messages[store.messages.length - 1];
    expect(last.content).not.toContain('[User stopped this output]');

    // The cancelled frame arrives: finalize marks the turn stopped.
    store.finalizeStopped();
    expect(store.isStopping).toBe(false);
    expect(store.isStreaming).toBe(false);
    const finalized = store.messages[store.messages.length - 1];
    expect(finalized.content).toContain('[User stopped this output]');
    const toolStep = finalized.steps?.find(
      (s) => s.type === 'tool_call' && s.id === 'tc-1',
    );
    expect(toolStep && 'status' in toolStep ? toolStep.status : undefined).toBe(
      'cancelled',
    );
  });

  it('finalizeStopped leaves legacy message.toolCalls untouched (steps-only store)', () => {
    // The mobile store never writes the legacy toolCalls field (render-time
    // derivation from steps, pinned by test_mobile_legacy_message_fields.py).
    // A stepless history record passing through finalizeStopped must come
    // out with its toolCalls array identical, not rewritten.
    const legacyToolCalls = [
      {
        id: 'tc-9',
        name: 'slow_tool',
        arguments: {},
        status: 'running' as const,
        startTime: new Date('2026-05-17T12:00:02Z'),
      },
    ];
    store.setMessages([
      makeUser('go'),
      {
        id: 'streamed',
        role: 'assistant',
        content: 'partial',
        timestamp: new Date('2026-05-17T12:00:02Z'),
        status: 'streaming',
        steps: [],
        toolCalls: legacyToolCalls,
      },
    ]);
    store.setStreaming(true);

    store.finalizeStopped();

    const finalized = store.messages[store.messages.length - 1];
    expect(finalized.toolCalls).toBe(legacyToolCalls);
    expect(finalized.content).toContain('[User stopped this output]');
  });

  it('idle response finalizes immediately', async () => {
    vi.mocked(api.stopThread).mockResolvedValue(stopResult({ status: 'idle' }));
    seedStreamingTurn();

    await store.stopGenerating('t1');

    expect(store.isStopping).toBe(false);
    expect(store.isStreaming).toBe(false);
    expect(abortCurrentStream).toHaveBeenCalled();
  });

  it('restores server texts plus local sending entries to the composer', async () => {
    vi.mocked(api.stopThread).mockResolvedValue(
      stopResult({
        restoredPrompts: [
          { text: 'server queued', sourceLabel: 'U', userId: 'u1', enqueuedAt: 1 },
        ],
      })
    );
    seedStreamingTurn();
    const queuedId = store.addPendingPrompt('server queued');
    store.setPendingPromptStatus(queuedId, 'queued');
    store.addPendingPrompt('still sending');

    await store.stopGenerating('t1');

    expect(store.composerRestore).toBe('server queued\n---\nstill sending');
    expect(store.pendingPrompts).toHaveLength(0);
  });

  it('falls back to a local stop when the backend is unreachable', async () => {
    vi.mocked(api.stopThread).mockRejectedValue(new Error('down'));
    seedStreamingTurn();
    store.addPendingPrompt('local only');

    await store.stopGenerating('t1');

    expect(store.isStopping).toBe(false);
    expect(store.isStreaming).toBe(false);
    expect(abortCurrentStream).toHaveBeenCalled();
    expect(store.composerRestore).toBe('local only');
    expect(store.pendingPrompts).toHaveLength(0);
  });

  it('fallback timer force-finalizes when no cancelled frame arrives', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.stopThread).mockResolvedValue(stopResult());
      seedStreamingTurn();

      await store.stopGenerating('t1');
      expect(store.isStopping).toBe(true);

      vi.advanceTimersByTime(8_000);

      expect(store.isStopping).toBe(false);
      expect(store.isStreaming).toBe(false);
      expect(abortCurrentStream).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('second stop tap is a no-op while stopping', async () => {
    vi.mocked(api.stopThread).mockResolvedValue(stopResult());
    seedStreamingTurn();

    await store.stopGenerating('t1');
    await store.stopGenerating('t1');

    expect(vi.mocked(api.stopThread)).toHaveBeenCalledTimes(1);
  });

  it('restoreToComposer joins with --- and consume clears the channel', () => {
    store.restoreToComposer(['first', 'second']);
    store.restoreToComposer(['third']);
    expect(store.composerRestore).toBe('first\n---\nsecond\n---\nthird');
    expect(store.consumeComposerRestore()).toBe('first\n---\nsecond\n---\nthird');
    expect(store.composerRestore).toBe('');
  });

  it('restores server prompts even when the cancelled frame finalizes during the stop await', async () => {
    // The POST /stop response and the SSE cancelled frame race over two
    // connections; when the frame wins, the response is still the only
    // copy of the restored texts the initiating client gets.
    let resolveStop!: (r: StopThreadResult) => void;
    vi.mocked(api.stopThread).mockImplementation(
      () => new Promise<StopThreadResult>((resolve) => { resolveStop = resolve; })
    );
    seedStreamingTurn();
    const queuedId = store.addPendingPrompt('queued text');
    store.setPendingPromptStatus(queuedId, 'queued');

    const stopPromise = store.stopGenerating('t1');
    // Cancelled frame arrives first: finalize + the mirror stream's
    // restored error removes the pending chip.
    store.finalizeStopped();
    store.removePendingPrompt(queuedId);
    expect(store.isStopping).toBe(false);

    resolveStop(
      stopResult({
        restoredPrompts: [
          { text: 'queued text', sourceLabel: 'U', userId: 'u1', enqueuedAt: 1 },
        ],
      })
    );
    await stopPromise;

    expect(store.composerRestore).toBe('queued text');
    // Already finalized: the late response must not re-abort a stream.
    expect(abortCurrentStream).not.toHaveBeenCalled();
  });

  it('late stop response does not duplicate texts the fallback already restored', async () => {
    vi.useFakeTimers();
    try {
      let resolveStop!: (r: StopThreadResult) => void;
      vi.mocked(api.stopThread).mockImplementation(
        () => new Promise<StopThreadResult>((resolve) => { resolveStop = resolve; })
      );
      seedStreamingTurn();
      const queuedId = store.addPendingPrompt('only once');
      store.setPendingPromptStatus(queuedId, 'queued');

      const stopPromise = store.stopGenerating('t1');
      vi.advanceTimersByTime(8_000); // fallback restores the local copy

      expect(store.composerRestore).toBe('only once');

      resolveStop(
        stopResult({
          restoredPrompts: [
            { text: 'only once', sourceLabel: 'U', userId: 'u1', enqueuedAt: 1 },
          ],
        })
      );
      await stopPromise;

      expect(store.composerRestore).toBe('only once');
    } finally {
      vi.useRealTimers();
    }
  });

  it('finalize via the cancelled frame cancels the fallback timer', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.stopThread).mockResolvedValue(stopResult());
      seedStreamingTurn();

      await store.stopGenerating('t1');
      store.finalizeStopped(); // the cancelled frame arrives

      vi.mocked(abortCurrentStream).mockClear();
      vi.advanceTimersByTime(8_000);

      expect(abortCurrentStream).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('clearMessages resets stop state and drops an unconsumed composer restore', async () => {
    vi.mocked(api.stopThread).mockResolvedValue(stopResult());
    seedStreamingTurn();
    await store.stopGenerating('t1');
    store.restoreToComposer(['leftover']);

    store.clearMessages();

    expect(store.isStopping).toBe(false);
    expect(store.composerRestore).toBe('');
  });
});

describe('chatStore: action sheet state (mobile)', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('openActionSheet/closeActionSheet set and clear the target id', () => {
    expect(store.actionSheetMessageId).toBeNull();
    store.openActionSheet('u2');
    expect(store.actionSheetMessageId).toBe('u2');
    store.closeActionSheet();
    expect(store.actionSheetMessageId).toBeNull();
  });

  it('prepareForThreadSwitch closes an open action sheet', () => {
    store.openActionSheet('u2');
    store.prepareForThreadSwitch();
    expect(store.actionSheetMessageId).toBeNull();
  });

  it('clearMessages closes an open action sheet (new-thread flow)', () => {
    store.openActionSheet('u2');
    store.clearMessages();
    expect(store.actionSheetMessageId).toBeNull();
  });
});

describe('chatStore: interactive-turn recovery (re-attachable turns)', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  function seedStreamingTurn() {
    store.setMessages([makeUser('hello')]);
    store.addAssistantMessage();
    store.setStreaming(true);
    store.addThinkingStep('working on it');
    store.addToolCallStep('tc-1', 'web_search', { q: 'x' });
    store.addResponseStep('partial ans');
    store.flushStreamingBuffers();
  }

  it('setReconnecting flags recovery and keeps the tail streaming', () => {
    seedStreamingTurn();

    store.setReconnecting(true);

    expect(store.isReconnecting).toBe(true);
    const tail = store.messages[store.messages.length - 1];
    expect(tail.status).toBe('streaming');

    store.setReconnecting(false);
    expect(store.isReconnecting).toBe(false);
  });

  it('setReconnecting discards un-flushed stream buffers', () => {
    seedStreamingTurn();
    store.addResponseStep(' BUFFERED AFTER DROP');

    store.setReconnecting(true);
    store.flushStreamingBuffers();

    const tail = store.messages[store.messages.length - 1];
    const responseText = (tail.steps || [])
      .filter((s) => s.type === 'response')
      .map((s) => s.content)
      .join('');
    expect(responseText).toBe('partial ans');
  });

  it('resetLastMessageForReplay clears the tail for a full-turn replay', () => {
    seedStreamingTurn();

    store.resetLastMessageForReplay();

    const tail = store.messages[store.messages.length - 1];
    expect(tail.role).toBe('assistant');
    expect(tail.status).toBe('streaming');
    expect(tail.content).toBe('');
    expect(tail.steps).toEqual([]);
    expect(store.activeToolCalls.size).toBe(0);
    // The user message is untouched.
    expect(store.messages[0].content).toBe('hello');
  });

  it('resetLastMessageForReplay then replay rebuilds the reply from stream events', () => {
    seedStreamingTurn();

    store.resetLastMessageForReplay();
    store.addThinkingStep('replayed thinking');
    store.addResponseStep('full replayed answer');
    store.flushStreamingBuffers();
    store.reclassifyThinkingAsResponse();
    store.setLastMessageComplete();

    const tail = store.messages[store.messages.length - 1];
    expect(tail.status).toBe('complete');
    expect(tail.content).toBe('full replayed answer');
  });

  it('resetLastMessageForReplay no-ops when the tail is not an assistant message', () => {
    store.setMessages([makeUser('hello')]);
    const snapshot = structuredClone(store.messages);

    store.resetLastMessageForReplay();

    expect(store.messages).toEqual(snapshot);
  });
});

describe('chatStore: live-attach viewer support (backlog #87)', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  function makeGraphUser(content: string, graphMessageId: string, id = `u-${graphMessageId}`): Message {
    return { ...makeUser(content, id), graphMessageId };
  }

  it('requestViewerAttach publishes a monotonically-sequenced request', () => {
    expect(store.viewerAttachRequest).toBeNull();

    store.requestViewerAttach('t-1', {
      turnId: 'turn-a',
      state: 'live',
      lastSeq: 7,
      truncated: false,
      userMessageId: 'msg-anchor',
    });
    const first = store.viewerAttachRequest;
    expect(first).toMatchObject({
      threadId: 't-1',
      turnId: 'turn-a',
      userMessageId: 'msg-anchor',
    });

    store.requestViewerAttach('t-1', {
      turnId: 'turn-b',
      state: 'live',
      lastSeq: 2,
      truncated: false,
    });
    const second = store.viewerAttachRequest;
    expect(second?.seq).toBeGreaterThan(first?.seq ?? 0);
    expect(second?.turnId).toBe('turn-b');
    // Missing anchor (message-less /resume turn) normalizes to null.
    expect(second?.userMessageId).toBeNull();
  });

  it('trimAfterGraphMessageId drops the hydrated turn-so-far after the anchor', () => {
    store.setMessages([
      makeGraphUser('earlier prompt', 'g-1'),
      makeCompletedAssistant('earlier reply'),
      makeGraphUser('live turn prompt', 'g-2', 'u-live'),
      makeCompletedAssistant('partial turn render', 'partial-1'),
    ]);

    expect(store.trimAfterGraphMessageId('g-2')).toBe(true);

    expect(store.messages).toHaveLength(3);
    const tail = store.messages[store.messages.length - 1];
    expect(tail.graphMessageId).toBe('g-2');
    // Earlier turns are untouched.
    expect(store.messages[0].graphMessageId).toBe('g-1');
    expect(store.messages[1].content).toBe('earlier reply');
  });

  it('trimAfterGraphMessageId is a no-op when the anchor is already the tail', () => {
    store.setMessages([
      makeGraphUser('earlier prompt', 'g-1'),
      makeGraphUser('live turn prompt', 'g-2'),
    ]);

    expect(store.trimAfterGraphMessageId('g-2')).toBe(true);
    expect(store.messages).toHaveLength(2);
  });

  it('trimAfterGraphMessageId returns false and trims nothing when the anchor is unknown', () => {
    store.setMessages([
      makeGraphUser('earlier prompt', 'g-1'),
      makeCompletedAssistant('earlier reply'),
    ]);

    expect(store.trimAfterGraphMessageId('g-missing')).toBe(false);
    expect(store.messages).toHaveLength(2);
  });

  // Backlog #90 slice 2: autonomous turns are watched through the same
  // viewer path; the anchor may be an invisible hidden-wakeup stub.

  it('requestViewerAttach carries holder metadata and defaults it for older backends', () => {
    store.requestViewerAttach('t-auto', {
      turnId: 'turn-auto',
      state: 'live',
      lastSeq: 3,
      truncated: false,
      userMessageId: 'msg-wake',
      holderKind: 'autonomous',
      sourceLabel: 'daily report',
      userMessageInternal: true,
    });
    expect(store.viewerAttachRequest).toMatchObject({
      holderKind: 'autonomous',
      sourceLabel: 'daily report',
    });

    store.requestViewerAttach('t-legacy', {
      turnId: 'turn-legacy',
      state: 'live',
      lastSeq: 1,
      truncated: false,
    });
    expect(store.viewerAttachRequest).toMatchObject({
      holderKind: 'user',
      sourceLabel: null,
    });
  });

  it('trimAfterGraphMessageId anchors on a hidden wakeup stub', () => {
    store.setMessages([
      makeGraphUser('earlier prompt', 'g-1'),
      makeCompletedAssistant('earlier reply'),
      { ...makeGraphUser('', 'g-wake', 'u-stub'), hidden: true, content: '' },
      makeCompletedAssistant('persisted turn-so-far', 'partial-1'),
    ]);

    expect(store.trimAfterGraphMessageId('g-wake')).toBe(true);
    expect(store.messages).toHaveLength(3);
    const tail = store.messages[store.messages.length - 1];
    expect(tail.hidden).toBe(true);
    expect(tail.graphMessageId).toBe('g-wake');
  });

  it('setBufferAttachedThread marks and clears the buffer-rendered thread', () => {
    expect(store.bufferAttachedThreadId).toBeNull();
    store.setBufferAttachedThread('t-watch');
    expect(store.bufferAttachedThreadId).toBe('t-watch');
    store.setBufferAttachedThread(null);
    expect(store.bufferAttachedThreadId).toBeNull();
  });

});

describe('chatStore: turn-paused card + resume request (backlog #27)', () => {
  let store: ReturnType<typeof createChatStore>;

  const pausedInfo = {
    reason: 'max_iterations',
    scope: 'main_agent',
    content: 'I reached the maximum number of steps (500) and had to stop.',
    maxIterations: 500,
    toolCallCount: 501,
    resumable: true,
  };

  beforeEach(() => {
    store = createChatStore();
  });

  it('handleTurnPaused completes the streaming reply and appends the card message', () => {
    store.addUserMessage('do the big task');
    store.addAssistantMessage();
    store.appendToLastMessage('working on it');

    store.handleTurnPaused({ ...pausedInfo });

    const messages = store.messages;
    const card = messages[messages.length - 1];
    expect(card.turnPausedInfo).toMatchObject({
      reason: 'max_iterations',
      resumable: true,
    });
    // The card message is complete: the turn ended at the halt.
    expect(card.status).toBe('complete');
    // The preceding assistant reply was finalized, not left streaming.
    expect(messages[messages.length - 2].status).toBe('complete');
  });

  it('markTurnPausedResumed flips only the latest un-resumed card', () => {
    store.handleTurnPaused({ ...pausedInfo });
    store.handleTurnPaused({ ...pausedInfo });

    store.markTurnPausedResumed();

    const cards = store.messages.filter((m) => m.turnPausedInfo);
    expect(cards[0].turnPausedInfo?.resumed).toBeUndefined();
    expect(cards[1].turnPausedInfo?.resumed).toBe(true);

    store.markTurnPausedResumed();
    const after = store.messages.filter((m) => m.turnPausedInfo);
    expect(after[0].turnPausedInfo?.resumed).toBe(true);
  });

  it('requestResume bumps the consumer-facing counter', () => {
    expect(store.resumeRequest).toBe(0);
    store.requestResume();
    store.requestResume();
    expect(store.resumeRequest).toBe(2);
  });
});

describe('chatStore: LLM fallback consent card (llm-fallback-consent Phase 2)', () => {
  let store: ReturnType<typeof createChatStore>;

  const promptInfo = {
    recordId: 'fb-1',
    kind: 'refusal',
    fromProvider: 'anthropic',
    fromModel: 'claude-fable-5',
    toProvider: 'anthropic',
    toModel: 'claude-opus-4-8',
    reason: 'refusal',
    httpStatus: null,
    timeoutSeconds: 180,
    holdOptions: [600, 3600, 7200, 28800],
    allowPermanent: true,
    defaultHoldSeconds: 7200,
    createdAt: '2026-07-25T00:00:00+00:00',
    expiresAt: '2026-07-25T00:03:00+00:00',
  };

  beforeEach(() => {
    store = createChatStore();
  });

  it('handleFallbackPrompt completes the streaming reply and appends a streaming carrier', () => {
    store.addUserMessage('do the task');
    store.addAssistantMessage();
    store.appendToLastMessage('partial output');

    store.handleFallbackPrompt({ ...promptInfo });

    const messages = store.messages;
    const card = messages[messages.length - 1];
    expect(card.fallbackPromptInfo).toMatchObject({
      recordId: 'fb-1',
      kind: 'refusal',
      toModel: 'claude-opus-4-8',
    });
    // The carrier stays STREAMING: after the swap the retried model's
    // output continues into this bubble, right under the card.
    expect(card.status).toBe('streaming');
    // The preceding assistant reply was finalized, not left streaming.
    expect(messages[messages.length - 2].status).toBe('complete');
  });

  it('post-swap continuation streams into the carrier bubble', () => {
    store.addUserMessage('do the task');
    store.addAssistantMessage();
    store.handleFallbackPrompt({ ...promptInfo });

    store.appendToLastMessage('continued on the fallback model');
    store._forceFlush();

    const card = store.messages[store.messages.length - 1];
    expect(card.fallbackPromptInfo?.recordId).toBe('fb-1');
    expect(card.content).toContain('continued on the fallback model');
  });

  it('resolveFallbackPromptCard stamps the outcome once; first resolution wins', () => {
    store.handleFallbackPrompt({ ...promptInfo });

    store.resolveFallbackPromptCard('fb-1', {
      outcome: 'approved',
      holdSeconds: 7200,
      holdPermanent: false,
    });
    const card = store.messages.find((m) => m.fallbackPromptInfo);
    expect(card?.fallbackPromptInfo?.resolved).toMatchObject({
      outcome: 'approved',
      holdSeconds: 7200,
    });

    // A later SSE flip (e.g. the timeout event racing an optimistic clear)
    // must not overwrite the first outcome.
    store.resolveFallbackPromptCard('fb-1', { outcome: 'timeout' });
    const after = store.messages.find((m) => m.fallbackPromptInfo);
    expect(after?.fallbackPromptInfo?.resolved?.outcome).toBe('approved');
  });

  it('rewindLastAssistantToStablePoint trims the failed attempt behind an empty carrier', () => {
    // Ask-mode post-chunk recovery: the failed partial output lives in the
    // bubble BEHIND the consent card's still-empty carrier; the rewound
    // provider_fallback must trim that bubble, not the carrier.
    store.addUserMessage('do the task');
    store.addAssistantMessage();
    store.appendToLastMessage('doomed partial output');
    store._forceFlush();
    store.handleFallbackPrompt({ ...promptInfo, kind: 'transport', reason: 'overloaded' });

    store.rewindLastAssistantToStablePoint();

    const messages = store.messages;
    const card = messages[messages.length - 1];
    // The carrier (and its card) survives; the re-drive streams into it.
    expect(card.fallbackPromptInfo?.recordId).toBe('fb-1');
    expect(card.status).toBe('streaming');
    // The failed attempt's text was trimmed from the previous bubble.
    const attempt = messages[messages.length - 2];
    expect(attempt.role).toBe('assistant');
    expect(attempt.content).toBe('');
    expect(attempt.steps ?? []).toHaveLength(0);
  });

  it('resolveFallbackPromptCard no-ops on an unknown record id', () => {
    store.handleFallbackPrompt({ ...promptInfo });
    const snapshot = structuredClone(store.messages);

    store.resolveFallbackPromptCard('fb-unknown', { outcome: 'declined' });

    expect(store.messages).toEqual(snapshot);
  });
});

describe('chatStore: command levels + structured turn errors (backlog #135 + #98)', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('addCommandResult stores the wire level', () => {
    store.addCommandResult('/env set', 'Set with a caveat.', true, 'warning');

    const msg = store.messages[0];
    expect(msg.kind).toBe('command_result');
    expect(msg.commandLevel).toBe('warning');
    expect(msg.status).toBe('complete');
  });

  it('addCommandResult derives the level from the success boolean when no level rides', () => {
    store.addCommandResult('/model x', 'Done.', true);
    store.addCommandResult('/model y', 'No such model.', false);

    expect(store.messages[0].commandLevel).toBe('success');
    expect(store.messages[0].status).toBe('complete');
    expect(store.messages[1].commandLevel).toBe('error');
    expect(store.messages[1].status).toBe('error');
  });

  it('setLastMessageError stores structured errorText and leaves content/steps untouched', () => {
    store.addUserMessage('hi');
    store.addAssistantMessage();
    store.appendToLastMessage('partial reply');
    store._forceFlush();

    store.setLastMessageError('Could not reach the model. (code: upstream)');

    const tail = store.messages[store.messages.length - 1];
    expect(tail.status).toBe('error');
    expect(tail.errorText).toBe('Could not reach the model. (code: upstream)');
    // The old idiom appended "**Error:** ..." markdown into BOTH content and
    // steps; the alert block renders errorText, so neither may carry it.
    expect(tail.content).toBe('partial reply');
    const stepText = (tail.steps ?? []).map((s) => s.content ?? '').join('');
    expect(stepText).not.toContain('**Error:**');
  });

  it('setLastMessageError falls back to the default copy for an empty message', () => {
    store.addAssistantMessage();
    store.setLastMessageError('');
    expect(store.messages[0].errorText).toBe('The reply could not be completed.');
  });

  it('setLastMessageError no-ops when the tail is not an assistant message', () => {
    store.addCommandResult('/x', 'Done.', true);
    const snapshot = structuredClone(store.messages);

    store.setLastMessageError('Boom');

    expect(store.messages).toEqual(snapshot);
  });

  it('completion after an error preserves the error state', () => {
    store.addAssistantMessage();
    store.setLastMessageError('Boom');
    store.setLastMessageComplete();

    const tail = store.messages[store.messages.length - 1];
    expect(tail.status).toBe('error');
    expect(tail.errorText).toBe('Boom');
  });

  it('resetLastMessageForReplay clears stale errorText for the rebuilt turn', () => {
    store.addAssistantMessage();
    store.setLastMessageError('Boom');

    store.resetLastMessageForReplay();

    const tail = store.messages[store.messages.length - 1];
    expect(tail.errorText).toBeUndefined();
    expect(tail.status).toBe('streaming');
  });
});
