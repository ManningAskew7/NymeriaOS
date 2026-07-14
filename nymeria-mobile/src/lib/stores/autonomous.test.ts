import { describe, expect, it } from 'vitest';

import {
  MAX_BUFFERED_EVENTS_PER_THREAD,
  MAX_BUFFERED_CHARS_PER_THREAD,
  markBufferOverflowIfNeeded,
  type PendingBuffer,
} from './autonomous.svelte';

/**
 * Per-thread replay-buffer cap (backlog #89). Mirrors the desktop store's
 * `markBufferOverflowIfNeeded` coverage: the pure overflow decision is the
 * genuinely new logic mobile gained, so it is what we pin. The buffer/replay
 * wiring around it is closure-internal and covered by `npm run check` (types).
 */

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
