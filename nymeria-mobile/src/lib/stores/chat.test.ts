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
