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
