import { describe, expect, it, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  getThreadHistory: vi.fn(),
  rewindThread: vi.fn(),
  truncateFromMessage: vi.fn(),
  setMessages: vi.fn(),
  refreshThreadSyncBaseline: vi.fn(),
  chatMessages: [] as unknown[],
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getThreadHistory: mocks.getThreadHistory,
    rewindThread: mocks.rewindThread,
  },
}));

vi.mock('$lib/stores/chat.svelte', () => ({
  chatStore: {
    get messages() {
      return mocks.chatMessages;
    },
    truncateFromMessage: mocks.truncateFromMessage,
    setMessages: mocks.setMessages,
  },
}));

vi.mock('$lib/stores/syncPoll.svelte', () => ({
  refreshThreadSyncBaseline: mocks.refreshThreadSyncBaseline,
}));

import {
  computeVisibleBlastRadius,
  isCountableUserMessage,
  resolveRewindTarget,
  rewindToMessage,
} from './rewind';
import type { Message } from '$lib/types';

function makeMessage(overrides: Partial<Message> & { id: string }): Message {
  return {
    role: 'user',
    content: 'hello',
    timestamp: new Date('2026-07-05T10:00:00Z'),
    status: 'complete',
    ...overrides,
  } as Message;
}

beforeEach(() => {
  mocks.getThreadHistory.mockReset();
  mocks.rewindThread.mockReset();
  mocks.truncateFromMessage.mockReset();
  mocks.setMessages.mockReset();
  mocks.refreshThreadSyncBaseline.mockReset();
  mocks.refreshThreadSyncBaseline.mockResolvedValue(undefined);
  mocks.chatMessages = [];
});

describe('isCountableUserMessage', () => {
  it('counts plain user prompts and skips assistant and autonomous messages', () => {
    expect(isCountableUserMessage(makeMessage({ id: 'u1' }))).toBe(true);
    expect(isCountableUserMessage(makeMessage({ id: 'a1', role: 'assistant' }))).toBe(false);
    expect(
      isCountableUserMessage(makeMessage({ id: 'u2', autonomousSource: 'scheduler' }))
    ).toBe(false);
  });
});

describe('computeVisibleBlastRadius', () => {
  const transcript = [
    makeMessage({ id: 'u1' }),
    makeMessage({ id: 'a1', role: 'assistant' }),
    makeMessage({ id: 'u2' }),
    makeMessage({ id: 'a2', role: 'assistant' }),
  ];

  it('counts messages after the target', () => {
    expect(computeVisibleBlastRadius(transcript, 0)).toBe(3);
    expect(computeVisibleBlastRadius(transcript, 2)).toBe(1);
    expect(computeVisibleBlastRadius(transcript, 3)).toBe(0);
  });

  it('is zero for out-of-range targets', () => {
    expect(computeVisibleBlastRadius(transcript, -1)).toBe(0);
    expect(computeVisibleBlastRadius([], 0)).toBe(0);
  });
});

describe('resolveRewindTarget', () => {
  it('returns the graph id directly for hydrated bubbles without fetching', async () => {
    const local = [makeMessage({ id: 'c1', graphMessageId: 'graph-h1' })];
    const resolved = await resolveRewindTarget('t1', local, 'c1');
    expect(resolved).toBe('graph-h1');
    expect(mocks.getThreadHistory).not.toHaveBeenCalled();
  });

  it('returns null when the target is not in the local transcript', async () => {
    expect(await resolveRewindTarget('t1', [], 'nope')).toBeNull();
  });

  it('returns null for non-countable targets (autonomous prompts)', async () => {
    const local = [makeMessage({ id: 'c1', autonomousSource: 'trigger' })];
    expect(await resolveRewindTarget('t1', local, 'c1')).toBeNull();
  });

  it('maps live bubbles ordinally onto the authoritative fetch, skipping autonomous prompts', async () => {
    // Local view (autonomous prompts hidden by the display toggle):
    //   u1 (hydrated), a1, u2-live, a2, u3-live
    const local = [
      makeMessage({ id: 'c1', graphMessageId: 'graph-h1' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
      makeMessage({ id: 'c2' }),
      makeMessage({ id: 'a2', role: 'assistant' }),
      makeMessage({ id: 'c3' }),
    ];
    // Authoritative view adds an autonomous wake-up between u2 and u3.
    mocks.getThreadHistory.mockResolvedValue({
      threadId: 't1',
      messages: [
        makeMessage({ id: 'f1', graphMessageId: 'graph-h1' }),
        makeMessage({ id: 'fa1', role: 'assistant' }),
        makeMessage({ id: 'f2', graphMessageId: 'graph-h2' }),
        makeMessage({ id: 'fa2', role: 'assistant' }),
        makeMessage({
          id: 'f3',
          graphMessageId: 'graph-auto',
          autonomousSource: 'scheduler',
        }),
        makeMessage({ id: 'fa3', role: 'assistant' }),
        makeMessage({ id: 'f4', graphMessageId: 'graph-h3' }),
        makeMessage({ id: 'fa4', role: 'assistant' }),
      ],
    });

    // Target u2-live: 2nd countable user message from the end locally, so it
    // must map to graph-h2, not the autonomous graph-auto.
    const resolved = await resolveRewindTarget('t1', local, 'c2');
    expect(resolved).toBe('graph-h2');
    expect(mocks.getThreadHistory).toHaveBeenCalledWith('t1', {
      showAutonomousPrompts: true,
    });
  });

  it('returns null when the authoritative list has fewer prompts than expected', async () => {
    const local = [makeMessage({ id: 'c1' }), makeMessage({ id: 'c2' })];
    mocks.getThreadHistory.mockResolvedValue({
      threadId: 't1',
      messages: [makeMessage({ id: 'f1', graphMessageId: 'graph-h1' })],
    });
    expect(await resolveRewindTarget('t1', local, 'c1')).toBeNull();
  });

  it('tolerates server-side prefixes on the authoritative copy (containment, not equality)', async () => {
    const local = [makeMessage({ id: 'c1', content: 'what is the weather' })];
    mocks.getThreadHistory.mockResolvedValue({
      threadId: 't1',
      messages: [
        makeMessage({
          id: 'f1',
          graphMessageId: 'graph-h9',
          content: '[Context: Saturday 2026-07-05 10:00] what is the weather',
        }),
      ],
    });
    expect(await resolveRewindTarget('t1', local, 'c1')).toBe('graph-h9');
  });

  it('returns null when tail divergence shifts the ordinal map onto the wrong message', async () => {
    // A failed send left a local-only bubble after the target, inflating k by
    // one; the ordinal walk would land one message too early. The text check
    // must reject that match instead of rewinding an extra exchange.
    const local = [
      makeMessage({ id: 'c1', content: 'first question' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
      makeMessage({ id: 'c2', content: 'phantom send that never persisted' }),
    ];
    mocks.getThreadHistory.mockResolvedValue({
      threadId: 't1',
      messages: [
        makeMessage({ id: 'f0', graphMessageId: 'graph-h0', content: 'zeroth question' }),
        makeMessage({ id: 'fa0', role: 'assistant' }),
        makeMessage({ id: 'f1', graphMessageId: 'graph-h1', content: 'first question' }),
        makeMessage({ id: 'fa1', role: 'assistant' }),
      ],
    });
    expect(await resolveRewindTarget('t1', local, 'c1')).toBeNull();
  });
});

describe('rewindToMessage', () => {
  it('rewinds by graph id, truncates locally, and refreshes the sync baseline', async () => {
    mocks.chatMessages = [makeMessage({ id: 'c1', graphMessageId: 'graph-h1' })];
    mocks.rewindThread.mockResolvedValue({
      status: 'ok',
      thread_id: 't1',
      steps: 1,
      removed: 2,
    });

    const outcome = await rewindToMessage('t1', 'c1');

    expect(outcome).toEqual({ ok: true, removed: 2 });
    expect(mocks.rewindThread).toHaveBeenCalledWith('t1', { toMessageId: 'graph-h1' });
    expect(mocks.truncateFromMessage).toHaveBeenCalledWith('c1');
    expect(mocks.refreshThreadSyncBaseline).toHaveBeenCalledWith('t1');
  });

  it('reloads history and reports stale_target on backend 404', async () => {
    mocks.chatMessages = [makeMessage({ id: 'c1', graphMessageId: 'graph-h1' })];
    mocks.rewindThread.mockRejectedValue(
      new Error('Rewind target not found in thread state.')
    );
    mocks.getThreadHistory.mockResolvedValue({ threadId: 't1', messages: [] });

    const outcome = await rewindToMessage('t1', 'c1');

    expect(outcome.ok).toBe(false);
    expect(outcome.reason).toBe('stale_target');
    expect(mocks.setMessages).toHaveBeenCalledWith([]);
    expect(mocks.truncateFromMessage).not.toHaveBeenCalled();
  });

  it('reports busy without touching the transcript when the thread is locked', async () => {
    mocks.chatMessages = [makeMessage({ id: 'c1', graphMessageId: 'graph-h1' })];
    mocks.rewindThread.mockRejectedValue(new Error("Thread is busy (held by 'chat' for 3s)."));

    const outcome = await rewindToMessage('t1', 'c1');

    expect(outcome.ok).toBe(false);
    expect(outcome.reason).toBe('busy');
    expect(mocks.truncateFromMessage).not.toHaveBeenCalled();
    expect(mocks.setMessages).not.toHaveBeenCalled();
  });

  it('reports stale_target and reloads when the bubble cannot be resolved at all', async () => {
    mocks.chatMessages = [makeMessage({ id: 'c1' })];
    mocks.getThreadHistory
      .mockResolvedValueOnce({ threadId: 't1', messages: [] }) // authoritative fetch
      .mockResolvedValueOnce({ threadId: 't1', messages: [] }); // mismatch reload

    const outcome = await rewindToMessage('t1', 'c1');

    expect(outcome.ok).toBe(false);
    expect(outcome.reason).toBe('stale_target');
    expect(mocks.rewindThread).not.toHaveBeenCalled();
  });
});
