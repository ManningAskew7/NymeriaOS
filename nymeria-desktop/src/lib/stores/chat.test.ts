import { describe, expect, it, vi, beforeEach } from 'vitest';

// The chat store touches the api singleton (only in stopGenerating).
// Mock it so the heavy api/index.ts module chain isn't pulled in.
vi.mock('$lib/services/api.svelte', () => ({
  abortCurrentStream: vi.fn(),
  api: { stopThread: vi.fn().mockResolvedValue(undefined) },
}));

import { createChatStore } from './chat.svelte';
import type { Message } from '$lib/types';

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
