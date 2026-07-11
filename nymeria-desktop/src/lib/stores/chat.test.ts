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

describe('chatStore — completed-message immutability', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('addToolCallStep refuses to mutate a completed assistant tail', () => {
    const prev = makeCompletedAssistant('Previous turn reply.');
    store.setMessages([prev]);
    const snapshot = structuredClone(store.messages);

    store.addToolCallStep('tc-1', 'fake_tool', { x: 1 });

    expect(store.messages).toEqual(snapshot);
    expect(store.messages[0].steps).toEqual(prev.steps);
    expect(store.activeToolCalls.size).toBe(0);
  });

  it('addResponseStep refuses to mutate a completed assistant tail', () => {
    const prev = makeCompletedAssistant('Previous turn reply.');
    store.setMessages([prev]);
    const snapshot = structuredClone(store.messages);

    store.addResponseStep('NEW STREAM TEXT');
    store.flushStreamingBuffers();

    expect(store.messages).toEqual(snapshot);
    expect(store.messages[0].content).toBe('Previous turn reply.');
  });

  it('addThinkingStep refuses to mutate a completed assistant tail', () => {
    const prev = makeCompletedAssistant('Previous turn reply.');
    store.setMessages([prev]);
    const snapshot = structuredClone(store.messages);

    store.addThinkingStep('hmm let me think...');
    store.flushStreamingBuffers();

    expect(store.messages).toEqual(snapshot);
  });

  it('appendToLastMessage refuses to mutate a completed assistant tail', () => {
    const prev = makeCompletedAssistant('Previous turn reply.');
    store.setMessages([prev]);

    store.appendToLastMessage(' BAD APPEND');

    expect(store.messages[0].content).toBe('Previous turn reply.');
  });

  it('setLastMessageContent refuses to mutate a completed assistant tail', () => {
    const prev = makeCompletedAssistant('Previous turn reply.');
    store.setMessages([prev]);

    store.setLastMessageContent('OVERWRITE');

    expect(store.messages[0].content).toBe('Previous turn reply.');
  });

  it('setIntermediateContent refuses to mutate a completed assistant tail', () => {
    const prev = makeCompletedAssistant('Previous turn reply.');
    store.setMessages([prev]);

    store.setIntermediateContent('preamble text');

    expect(store.messages[0].intermediateContent).toBeUndefined();
  });

  it('reclassifyThinkingAsResponse refuses to overwrite content on a completed tail', () => {
    const prev: Message = {
      ...makeCompletedAssistant('Previous turn reply.'),
      steps: [
        { type: 'response', content: 'Previous turn reply.' },
        { type: 'response', content: 'DO NOT MERGE ME' },
      ],
    };
    store.setMessages([prev]);

    store.reclassifyThinkingAsResponse();

    expect(store.messages[0].content).toBe('Previous turn reply.');
  });
});

describe('chatStore — streaming positive controls', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('addToolCallStep mutates a streaming tail', () => {
    store.addAssistantMessage();

    store.addToolCallStep('tc-1', 'fake_tool', { x: 1 });

    const last = store.messages[store.messages.length - 1];
    expect(last.steps?.[0]).toMatchObject({
      type: 'tool_call',
      id: 'tc-1',
      name: 'fake_tool',
    });
    expect(store.activeToolCalls.has('tc-1')).toBe(true);
  });

  it('addResponseStep accumulates into the streaming tail', () => {
    store.addAssistantMessage();

    store.addResponseStep('Hello ');
    store.addResponseStep('world');
    store.flushStreamingBuffers();

    const last = store.messages[store.messages.length - 1];
    const responseSteps = last.steps?.filter((s) => s.type === 'response') || [];
    const joined = responseSteps.map((s) => s.content).join('');
    expect(joined).toBe('Hello world');
  });

  it('addThinkingStep accumulates into the streaming tail', () => {
    store.addAssistantMessage();

    store.addThinkingStep('think ');
    store.addThinkingStep('more');
    store.flushStreamingBuffers();

    const last = store.messages[store.messages.length - 1];
    const thinkingSteps = last.steps?.filter((s) => s.type === 'thinking') || [];
    expect(thinkingSteps.map((s) => s.content).join('')).toBe('think more');
  });
});

describe('chatStore — handoff regression scenario', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('a new streaming turn does not graft onto a prior completed reply', () => {
    // Replicates the user-reported bug: thread A handed off to thread B once,
    // turn N completed (assistant reply persisted). Thread A hands off again,
    // user opens thread B to watch. History returns the persisted previous
    // turn at the tail; the navigation recovery path now adds a fresh
    // assistant message instead of reusing the completed one, and any
    // events that managed to fire against the completed message are no-ops.
    const previousUser = makeUser('Original prompt from thread A', 'u-prev');
    const previousReply = makeCompletedAssistant(
      'Thread B reply to first handoff.',
      'a-prev',
    );
    store.setMessages([previousUser, previousReply]);

    // Simulate one stray streaming event that arrives before the new
    // assistant placeholder exists — guards must drop it.
    store.addResponseStep('STRAY EVENT BEFORE PLACEHOLDER');
    store.flushStreamingBuffers();
    expect(store.messages[1].content).toBe('Thread B reply to first handoff.');
    expect(
      store.messages[1].steps?.filter((s) => s.type === 'response').length,
    ).toBe(1);

    // Now navigation creates a fresh assistant message for the new turn
    // (this mirrors the navigation.svelte.ts recovery fix).
    const newId = store.addAssistantMessage();
    store.setStreaming(true);

    // New turn streams in: tool call, then response text.
    store.addToolCallStep('tc-new', 'spawn_thread', { mode: 'handoff' });
    store.addResponseStep('Reply to second handoff.');
    store.flushStreamingBuffers();
    store.reclassifyThinkingAsResponse();
    store.setLastMessageComplete();
    store.setStreaming(false);

    // Previous turn is untouched.
    expect(store.messages[0]).toEqual(previousUser);
    expect(store.messages[1].id).toBe('a-prev');
    expect(store.messages[1].content).toBe('Thread B reply to first handoff.');
    expect(store.messages[1].steps).toEqual(previousReply.steps);
    expect(store.messages[1].toolCalls).toEqual([]);

    // New turn lives in the new message.
    expect(store.messages[2].id).toBe(newId);
    expect(store.messages[2].content).toBe('Reply to second handoff.');
    const newSteps = store.messages[2].steps ?? [];
    expect(newSteps.some((s) => s.type === 'tool_call' && s.id === 'tc-new')).toBe(
      true,
    );
    expect(
      newSteps.some(
        (s) => s.type === 'response' && s.content === 'Reply to second handoff.',
      ),
    ).toBe(true);
  });

  it('without the guard, response steps would have grafted onto the prior reply (sanity check)', () => {
    // Documents the OLD buggy behavior by bypassing the guard: we force the
    // buggy state where a completed prior-turn message has its status flipped
    // back to 'streaming' (which is exactly what the old recovery code did).
    //
    // The shape mirrors the real-world wire format produced by
    // _handle_ai_history_message in nymeria/core/agent_history.py: a plain
    // assistant reply (no tool calls, no thinking) has `content` set but
    // `steps` empty/undefined. addToolCallStep and addResponseStep would
    // then graft new steps onto it, and reclassifyThinkingAsResponse would
    // overwrite `content` with just the new turn's response.
    const prev: Message = {
      id: 'a-prev',
      role: 'assistant',
      content: 'Original reply.',
      timestamp: new Date('2026-05-17T12:00:00Z'),
      status: 'streaming', // forced buggy state
      // No `steps` array — matches a plain text reply in history.
    };
    store.setMessages([prev]);

    store.addToolCallStep('tc-stray', 'fake_tool', {});
    store.addResponseStep('REPLACEMENT');
    store.flushStreamingBuffers();
    store.reclassifyThinkingAsResponse();

    // Tool call grafted in at top of the prior message:
    expect(
      store.messages[0].steps?.some(
        (s) => s.type === 'tool_call' && s.id === 'tc-stray',
      ),
    ).toBe(true);
    // `content` overwritten — exactly the visual "completely overwrote that
    // previous message" the user reported.
    expect(store.messages[0].content).toBe('REPLACEMENT');
  });
});

describe('chatStore — pending prompts queue', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('addPendingPrompt appends in FIFO order with status sending', () => {
    const id1 = store.addPendingPrompt('first');
    const id2 = store.addPendingPrompt('second');
    expect(store.pendingPrompts.map((p) => p.id)).toEqual([id1, id2]);
    expect(store.pendingPrompts.every((p) => p.status === 'sending')).toBe(true);
  });

  it('setPendingPromptStatus updates the matching entry only', () => {
    const id = store.addPendingPrompt('first');
    store.addPendingPrompt('second');
    store.setPendingPromptStatus(id, 'queued', undefined, 3);
    const updated = store.pendingPrompts.find((p) => p.id === id);
    expect(updated?.status).toBe('queued');
    expect(updated?.position).toBe(3);
    const other = store.pendingPrompts.find((p) => p.id !== id);
    expect(other?.status).toBe('sending');
  });

  it('consumeQueuedPrompts pops in FIFO order and skips errored entries', () => {
    const a = store.addPendingPrompt('a');
    const b = store.addPendingPrompt('b');
    const c = store.addPendingPrompt('c');
    store.setPendingPromptStatus(b, 'error', 'boom');

    const consumed = store.consumeQueuedPrompts(1);
    expect(consumed.map((p) => p.id)).toEqual([a]);
    // The errored entry remains, so does c.
    expect(store.pendingPrompts.map((p) => p.id)).toEqual([b, c]);
  });

  it('consumeQueuedPrompts caps at n', () => {
    store.addPendingPrompt('a');
    store.addPendingPrompt('b');
    store.addPendingPrompt('c');
    const consumed = store.consumeQueuedPrompts(2);
    expect(consumed.map((p) => p.content)).toEqual(['a', 'b']);
    expect(store.pendingPrompts.map((p) => p.content)).toEqual(['c']);
  });

  it('clearPendingPrompts empties the list', () => {
    store.addPendingPrompt('a');
    store.addPendingPrompt('b');
    store.clearPendingPrompts();
    expect(store.pendingPrompts).toHaveLength(0);
  });

  it('removePendingPrompt deletes by id', () => {
    const a = store.addPendingPrompt('a');
    const b = store.addPendingPrompt('b');
    store.removePendingPrompt(a);
    expect(store.pendingPrompts.map((p) => p.id)).toEqual([b]);
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

  it('finalizeStopped is idempotent', () => {
    seedStreamingTurn();
    store.finalizeStopped();
    const snapshot = structuredClone(store.messages);
    store.finalizeStopped();
    expect(store.messages).toEqual(snapshot);
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
    // 'queued' entries reached the backend (covered by the server list);
    // 'sending' entries never made it there and must be restored locally.
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

  it('second stop click is a no-op while stopping', async () => {
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

  it('done path clears stopping without cancelled visuals', async () => {
    vi.mocked(api.stopThread).mockResolvedValue(stopResult());
    seedStreamingTurn();

    await store.stopGenerating('t1');
    store.clearStopping();

    expect(store.isStopping).toBe(false);
    const last = store.messages[store.messages.length - 1];
    expect(last.content).not.toContain('[User stopped this output]');
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
