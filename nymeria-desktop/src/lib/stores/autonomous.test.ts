import { describe, expect, it, beforeEach } from 'vitest';

import {
  MAX_BUFFERED_EVENTS_PER_THREAD,
  MAX_BUFFERED_CHARS_PER_THREAD,
  markBufferOverflowIfNeeded,
  type PendingBuffer,
} from './autonomous.svelte';
import { createChatStore } from './chat.svelte';
import type { Message } from '$lib/types';

/**
 * Replay-on-join: when the user switches into a thread with an in-flight
 * autonomous turn, the autonomous store buffers the turn's streaming events
 * (bounded) and attachToThread binds a streaming chat message and replays them.
 *
 * The store is a singleton wired to many other stores, and its handleEvent /
 * buffer plumbing is closure-internal. We test the two genuinely new decisions
 * that ARE reachable: the pure buffer-overflow cap (exported), and attach's
 * graft-safe message reuse-vs-create rule against a real chat store. The full
 * wiring is covered by `npm run check` (types) and the manual E2E procedure in
 * the plan. If attachToThread's message rule in autonomous.svelte.ts changes,
 * update applyAutonomousAttach below to match (mirrors navigation.test.ts).
 */

// --- Buffer overflow cap -----------------------------------------------------

function freshBuffer(): PendingBuffer {
  return { events: [], chars: 0, overflowed: false };
}

describe('markBufferOverflowIfNeeded', () => {
  it('does not overflow an under-cap buffer', () => {
    const buf = freshBuffer();
    buf.events = new Array(10).fill({ type: 'response', thread_id: 't', task_id: 'a', timestamp: '' });
    buf.chars = 1000;
    expect(markBufferOverflowIfNeeded(buf, 't')).toBe(false);
    expect(buf.overflowed).toBe(false);
    expect(buf.events.length).toBe(10);
  });

  it('overflows and drops events at the event-count cap', () => {
    const buf = freshBuffer();
    buf.events = new Array(MAX_BUFFERED_EVENTS_PER_THREAD).fill({
      type: 'thinking', thread_id: 't', task_id: 'a', timestamp: '',
    });
    buf.chars = 5000;
    expect(markBufferOverflowIfNeeded(buf, 't')).toBe(true);
    expect(buf.overflowed).toBe(true);
    expect(buf.events).toEqual([]); // dropped to free memory
  });

  it('overflows at the char-budget cap even with few events', () => {
    const buf = freshBuffer();
    buf.events = [{ type: 'tool_result', thread_id: 't', task_id: 'a', timestamp: '' }];
    buf.chars = MAX_BUFFERED_CHARS_PER_THREAD;
    expect(markBufferOverflowIfNeeded(buf, 't')).toBe(true);
    expect(buf.overflowed).toBe(true);
    expect(buf.events).toEqual([]);
  });

  it('is idempotent once overflowed', () => {
    const buf: PendingBuffer = { events: [], chars: 0, overflowed: true };
    expect(markBufferOverflowIfNeeded(buf, 't')).toBe(true);
    expect(markBufferOverflowIfNeeded(buf, 't')).toBe(true);
  });

  it('stays just under the boundary', () => {
    const buf = freshBuffer();
    buf.events = new Array(MAX_BUFFERED_EVENTS_PER_THREAD - 1).fill({
      type: 'response', thread_id: 't', task_id: 'a', timestamp: '',
    });
    buf.chars = MAX_BUFFERED_CHARS_PER_THREAD - 1;
    expect(markBufferOverflowIfNeeded(buf, 't')).toBe(false);
    expect(buf.overflowed).toBe(false);
  });
});

// --- attachToThread message reuse-vs-create rule -----------------------------

/**
 * Mirrors attachToThread's chat-message decision in autonomous.svelte.ts: reuse
 * the tail only when it is an in-flight streaming assistant, else create a fresh
 * bubble; always mark streaming; set intermediate placeholder only when created.
 */
function applyAutonomousAttach(store: ReturnType<typeof createChatStore>): boolean {
  const lastMsg = store.messages[store.messages.length - 1];
  let created: boolean;
  if (lastMsg && lastMsg.role === 'assistant' && lastMsg.status === 'streaming') {
    store.setLastMessageStreaming();
    created = false;
  } else {
    store.addAssistantMessage();
    created = true;
  }
  store.setStreaming(true);
  if (created) {
    store.setIntermediateContent('Autonomous task in progress...');
  }
  return created;
}

function completedAssistant(id: string, content: string): Message {
  return {
    id,
    role: 'assistant',
    content,
    timestamp: new Date('2026-06-02T12:00:00Z'),
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
    timestamp: new Date('2026-06-02T12:00:00Z'),
    status: 'streaming',
    steps: [],
    toolCalls: [],
  };
}

function userMessage(id: string, content: string): Message {
  return { id, role: 'user', content, timestamp: new Date('2026-06-02T12:00:00Z'), status: 'complete' };
}

describe('attachToThread message binding (replay-on-join)', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('creates a NEW streaming bubble when the tail is a completed prior reply', () => {
    // Switching into a dream/autonomous thread whose history tail is the prior
    // committed turn: the in-flight turn must not graft onto it.
    store.setMessages([userMessage('u1', 'prompt'), completedAssistant('a1', 'prior reply')]);

    const created = applyAutonomousAttach(store);

    expect(created).toBe(true);
    expect(store.messages.length).toBe(3);
    expect(store.messages[1].id).toBe('a1');
    expect(store.messages[1].status).toBe('complete'); // prior reply intact
    expect(store.messages[2].role).toBe('assistant');
    expect(store.messages[2].status).toBe('streaming');
    expect(store.isStreaming).toBe(true);
    expect(store.messages[2].intermediateContent).toBe('Autonomous task in progress...');
  });

  it('REUSES the tail when it is an in-flight streaming assistant', () => {
    // History hydrated a processing turn as a streaming tail; attach binds to it
    // instead of opening a second bubble for the same turn.
    store.setMessages([userMessage('u1', 'prompt'), streamingAssistant('a-live')]);

    const created = applyAutonomousAttach(store);

    expect(created).toBe(false);
    expect(store.messages.length).toBe(2);
    expect(store.messages[1].id).toBe('a-live');
    expect(store.messages[1].status).toBe('streaming');
    expect(store.isStreaming).toBe(true);
  });

  it('creates a streaming bubble when history is empty', () => {
    store.setMessages([]);

    const created = applyAutonomousAttach(store);

    expect(created).toBe(true);
    expect(store.messages.length).toBe(1);
    expect(store.messages[0].status).toBe('streaming');
    expect(store.isStreaming).toBe(true);
  });

  it('replayed steps land on the fresh bubble and leave the prior reply intact', () => {
    store.setMessages([userMessage('u1', 'prompt'), completedAssistant('a1', 'prior reply')]);

    applyAutonomousAttach(store);
    // Simulate replaying buffered events in order.
    store.addToolCallStep('tc-1', 'memory_read', {});
    store.addResponseStep('Dream summary.');
    store.flushStreamingBuffers();
    store.reclassifyThinkingAsResponse();
    store.setLastMessageComplete();

    // Prior reply untouched.
    expect(store.messages[1].id).toBe('a1');
    expect(store.messages[1].content).toBe('prior reply');
    expect(store.messages[1].toolCalls).toEqual([]);
    expect(store.messages[1].steps).toEqual([{ type: 'response', content: 'prior reply' }]);
    // Replayed steps landed on the new bubble in order.
    const newSteps = store.messages[2].steps ?? [];
    expect(newSteps.some((s) => s.type === 'tool_call' && s.id === 'tc-1')).toBe(true);
    expect(newSteps.some((s) => s.type === 'response' && (s.content ?? '').includes('Dream summary.'))).toBe(true);
  });
});

// --- Stand-down during buffer attach (backlog #90 slice 2) -------------------

/**
 * Mirrors the guard at the top of canApplyStreamingEvent in
 * autonomous.svelte.ts: while the chat panel renders a thread from the turn
 * buffer (chatStore.bufferAttachedThreadId), bus transcript events for that
 * thread must not apply, even when a pre-attach binding is still present in
 * activeMessagesByThread. Update alongside autonomous.svelte.ts (same
 * contract note as applyAutonomousAttach above).
 */
describe('canApplyStreamingEvent stand-down during buffer attach', () => {
  function applyGuard(
    store: ReturnType<typeof createChatStore>,
    eventThreadId: string,
    isCurrentThread: boolean,
    isOurTask: boolean,
    hasBinding: boolean,
  ): boolean {
    if (store.bufferAttachedThreadId === eventThreadId) return false;
    return isCurrentThread && isOurTask && store.isStreaming && hasBinding;
  }

  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('applies transcript events normally when no buffer attach is active', () => {
    store.setStreaming(true);
    expect(applyGuard(store, 't-1', true, true, true)).toBe(true);
  });

  it('stands down for the buffer-attached thread even with a stale binding', () => {
    store.setStreaming(true);
    store.setBufferAttachedThread('t-1');
    expect(applyGuard(store, 't-1', true, true, true)).toBe(false);
    // Other threads are unaffected (their events stay buffered/applied by
    // the usual rules).
    expect(applyGuard(store, 't-2', true, true, true)).toBe(true);
  });

  it('resumes applying once the attach ends', () => {
    store.setStreaming(true);
    store.setBufferAttachedThread('t-1');
    store.setBufferAttachedThread(null);
    expect(applyGuard(store, 't-1', true, true, true)).toBe(true);
  });
});

/**
 * Mirrors the buffer-attach guards added alongside canApplyStreamingEvent
 * (backlog #90 review pass): bufferPendingEvent drops (never queues) events
 * for the buffer-attached thread, and the task_completed finalization block
 * requires bufferAttachedThreadId !== event.thread_id so a stale pre-attach
 * binding cannot finalize or error-paint the message mid-attach. Update
 * alongside autonomous.svelte.ts.
 */
describe('pending-buffer and task_completed stand-down during buffer attach', () => {
  function shouldQueuePending(
    store: ReturnType<typeof createChatStore>,
    eventThreadId: string,
  ): boolean {
    return store.bufferAttachedThreadId !== eventThreadId;
  }

  function shouldFinalizeFromTaskCompleted(
    store: ReturnType<typeof createChatStore>,
    eventThreadId: string,
    isCurrentThread: boolean,
    isOurTask: boolean,
    hadStreamingMessage: boolean,
  ): boolean {
    return (
      isCurrentThread &&
      isOurTask &&
      store.isStreaming &&
      hadStreamingMessage &&
      store.bufferAttachedThreadId !== eventThreadId
    );
  }

  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('queues pending transcript events when no attach is active', () => {
    expect(shouldQueuePending(store, 't-1')).toBe(true);
  });

  it('drops pending transcript events for the buffer-attached thread', () => {
    store.setBufferAttachedThread('t-1');
    expect(shouldQueuePending(store, 't-1')).toBe(false);
    // Other threads keep queueing for their own later switch-in replay.
    expect(shouldQueuePending(store, 't-2')).toBe(true);
  });

  it('task_completed finalizes normally when no attach is active', () => {
    store.setStreaming(true);
    expect(shouldFinalizeFromTaskCompleted(store, 't-1', true, true, true)).toBe(true);
  });

  it('task_completed does not finalize mid-attach despite a stale binding', () => {
    // The reviewer scenario: switch away and back during the same autonomous
    // turn leaves activeMessagesByThread bound while the buffer attach owns
    // rendering; the bus terminal event must not setStreaming(false) or
    // inject content under the replay.
    store.setStreaming(true);
    store.setBufferAttachedThread('t-1');
    expect(shouldFinalizeFromTaskCompleted(store, 't-1', true, true, true)).toBe(false);
    expect(shouldFinalizeFromTaskCompleted(store, 't-2', true, true, true)).toBe(true);
  });
});
