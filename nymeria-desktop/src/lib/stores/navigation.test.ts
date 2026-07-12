import { describe, expect, it, vi, beforeEach } from 'vitest';

vi.mock('$lib/services/api.svelte', () => ({
  abortCurrentStream: vi.fn(),
  api: { stopThread: vi.fn().mockResolvedValue(undefined) },
}));

import { createChatStore } from './chat.svelte';
import type { Message, ThreadStatus } from '$lib/types';

/**
 * Mirrors the stream-recovery decision in navigation.svelte.ts so we can
 * unit-test the branch without standing up the whole thread-switch pipeline
 * (which depends on threadsStore, autonomousStore, syncPoll, and the api
 * client). If navigation.svelte.ts changes shape, update this helper too.
 */
function applyStreamRecovery(store: ReturnType<typeof createChatStore>): void {
  const lastMsg = store.messages[store.messages.length - 1];
  if (lastMsg?.role === 'assistant' && lastMsg.status === 'streaming') {
    store.setLastMessageStreaming();
  } else {
    store.addAssistantMessage();
  }
  store.setStreaming(true);
}

function completedAssistant(id: string, content: string): Message {
  return {
    id,
    role: 'assistant',
    content,
    timestamp: new Date('2026-05-17T12:00:00Z'),
    status: 'complete',
    steps: [{ type: 'response', content }],
    toolCalls: [],
  };
}

function streamingAssistant(id: string): Message {
  return {
    id,
    role: 'assistant',
    content: '',
    timestamp: new Date('2026-05-17T12:00:00Z'),
    status: 'streaming',
    steps: [],
    toolCalls: [],
  };
}

function userMessage(id: string, content: string): Message {
  return {
    id,
    role: 'user',
    content,
    timestamp: new Date('2026-05-17T12:00:00Z'),
    status: 'complete',
  };
}

describe('navigation stream-recovery decision', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('adds a NEW assistant message when the tail is a completed prior reply', () => {
    // Replicates: handoff target thread is opened mid-stream after a prior
    // turn already completed.
    store.setMessages([
      userMessage('u1', 'first handoff prompt'),
      completedAssistant('a1', 'first reply'),
    ]);

    applyStreamRecovery(store);

    expect(store.messages.length).toBe(3);
    expect(store.messages[1].id).toBe('a1');
    expect(store.messages[1].status).toBe('complete'); // prior reply intact
    expect(store.messages[2].role).toBe('assistant');
    expect(store.messages[2].status).toBe('streaming');
    expect(store.messages[2].id).not.toBe('a1');
  });

  it('adds a NEW assistant message when history is empty', () => {
    store.setMessages([]);

    applyStreamRecovery(store);

    expect(store.messages.length).toBe(1);
    expect(store.messages[0].role).toBe('assistant');
    expect(store.messages[0].status).toBe('streaming');
  });

  it('adds a NEW assistant message when the tail is a user message', () => {
    store.setMessages([userMessage('u1', 'new handoff prompt')]);

    applyStreamRecovery(store);

    expect(store.messages.length).toBe(2);
    expect(store.messages[1].role).toBe('assistant');
    expect(store.messages[1].status).toBe('streaming');
  });

  it('REUSES the tail when it is already an in-flight streaming assistant', () => {
    // E.g. user navigates away from their own active stream and comes back —
    // the streaming placeholder we created locally is still at the tail.
    store.setMessages([
      userMessage('u1', 'prompt'),
      streamingAssistant('a-streaming'),
    ]);

    applyStreamRecovery(store);

    // Length unchanged; tail is the same id and still streaming.
    expect(store.messages.length).toBe(2);
    expect(store.messages[1].id).toBe('a-streaming');
    expect(store.messages[1].status).toBe('streaming');
  });

  it('viewer attach fires only when no local stream binds and a turn is live (backlog #87)', () => {
    // Mirrors switchToThread's three-way branch: own interactive stream >
    // autonomous attach > live-turn viewer attach. Update alongside
    // navigation.svelte.ts (same contract note as applyStreamRecovery).
    function applyAttachDecision(
      hasInteractiveStream: boolean,
      hasAutonomousTask: boolean,
      status: ThreadStatus,
    ): void {
      if (hasInteractiveStream || hasAutonomousTask) return;
      if (status.turn?.state === 'live') {
        store.requestViewerAttach(status.threadId, status.turn);
      }
    }

    const liveTurn: ThreadStatus = {
      threadId: 't-1',
      revision: 'r1',
      processing: true,
      turn: {
        turnId: 'turn-live',
        state: 'live',
        lastSeq: 3,
        truncated: false,
        userMessageId: 'g-1',
      },
    };

    applyAttachDecision(true, false, liveTurn);
    expect(store.viewerAttachRequest).toBeNull();

    applyAttachDecision(false, true, liveTurn);
    expect(store.viewerAttachRequest).toBeNull();

    // A finished (retained) turn buffer must not trigger a viewer attach.
    applyAttachDecision(false, false, {
      ...liveTurn,
      processing: false,
      turn: { ...liveTurn.turn!, state: 'done' },
    });
    expect(store.viewerAttachRequest).toBeNull();

    applyAttachDecision(false, false, liveTurn);
    expect(store.viewerAttachRequest).toMatchObject({
      threadId: 't-1',
      turnId: 'turn-live',
      userMessageId: 'g-1',
    });
  });

  it('streamed events flow into the fresh message and leave the prior intact', () => {
    store.setMessages([
      userMessage('u1', 'first handoff prompt'),
      completedAssistant('a1', 'first reply'),
    ]);

    applyStreamRecovery(store);
    store.addToolCallStep('tc-1', 'spawn_thread', { mode: 'handoff' });
    store.addResponseStep('Second reply content.');
    store.flushStreamingBuffers();
    store.reclassifyThinkingAsResponse();
    store.setLastMessageComplete();

    // Prior reply is intact — no grafted tool calls, no overwritten content.
    expect(store.messages[1].id).toBe('a1');
    expect(store.messages[1].content).toBe('first reply');
    expect(store.messages[1].toolCalls).toEqual([]);
    expect(store.messages[1].steps).toEqual([
      { type: 'response', content: 'first reply' },
    ]);

    // New reply has the new content.
    expect(store.messages[2].content).toBe('Second reply content.');
    expect(
      store.messages[2].steps?.some(
        (s) => s.type === 'tool_call' && s.id === 'tc-1',
      ),
    ).toBe(true);
  });
});
