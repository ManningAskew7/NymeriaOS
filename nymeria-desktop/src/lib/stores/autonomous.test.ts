import { describe, expect, it } from 'vitest';

import { shouldRequestViewerAttach } from './autonomous.svelte';
import { createChatStore } from './chat.svelte';
import type { ThreadTurnStatus } from '$lib/types';

/**
 * Autonomous turns render through the turn buffer attach path (backlog #90
 * slice 3): the store consumes bus events for lifecycle only and hands the
 * on-screen thread to the viewer attach on task_started / late-bound
 * turn-output signals. The attach decision is the exported pure gate
 * `shouldRequestViewerAttach` (imported here, so this tests the REAL rule,
 * not a mirror); the request/consume plumbing (requestViewerAttach,
 * bufferAttachedThreadId, watchLiveTurn) is covered by chat.test.ts and the
 * chat panel. The store singleton's SSE wiring is closure-internal and
 * covered by `npm run check` (types) plus the live verification procedure
 * in the slice 3 plan.
 */

function liveTurn(overrides: Partial<ThreadTurnStatus> = {}): ThreadTurnStatus {
  return {
    turnId: 'turn-live',
    state: 'live',
    lastSeq: 3,
    truncated: false,
    userMessageId: 'g-1',
    holderKind: 'autonomous',
    sourceLabel: 'daily report',
    userMessageInternal: true,
    ...overrides,
  };
}

describe('shouldRequestViewerAttach (autonomous signals → viewer attach)', () => {
  it('attaches a live, replayable turn on the open thread', () => {
    expect(shouldRequestViewerAttach(liveTurn(), true, false)).toBe(true);
  });

  it('never fires for a background thread', () => {
    expect(shouldRequestViewerAttach(liveTurn(), false, false)).toBe(false);
  });

  it('skips when this client is already streaming (own turn or active attach)', () => {
    expect(shouldRequestViewerAttach(liveTurn(), true, true)).toBe(false);
  });

  it('skips a truncated live turn (cannot replay; settles from history)', () => {
    expect(
      shouldRequestViewerAttach(liveTurn({ truncated: true }), true, false)
    ).toBe(false);
  });

  it('skips a finished turn (nothing live to watch)', () => {
    expect(
      shouldRequestViewerAttach(liveTurn({ state: 'done' }), true, false)
    ).toBe(false);
  });

  it('skips when the backend has no turn block (older backend)', () => {
    expect(shouldRequestViewerAttach(undefined, true, false)).toBe(false);
    expect(shouldRequestViewerAttach(null, true, false)).toBe(false);
  });
});

describe('viewer attach request is a signal, not a render', () => {
  it('requestViewerAttach does not itself set streaming', () => {
    // task_started must not flip the UI into streaming (the old firehose
    // behavior that starved the attach path): the request only records the
    // turn; the chat panel's watchLiveTurn consumer owns isStreaming.
    const store = createChatStore();
    store.requestViewerAttach('t-1', liveTurn());
    expect(store.isStreaming).toBe(false);
    expect(store.viewerAttachRequest).toMatchObject({
      threadId: 't-1',
      turnId: 'turn-live',
      userMessageId: 'g-1',
      holderKind: 'autonomous',
      sourceLabel: 'daily report',
    });
  });
});
