import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/config.svelte', () => ({ configStore: {} }));
vi.mock('./credentials', () => ({
  CredentialsApi: class {
    getBaseUrl() { return 'http://test'; }
    getHeaders() { return {}; }
  }
}));

import { ChatApi, consumeTurnStream } from './chat';

afterEach(() => vi.unstubAllGlobals());

describe('direct turn stream recovery', () => {
  it.each([false, true])('recovers a replay gap, including before its start frame (%s)', async (hasStart) => {
    const wire = [
      ...(hasStart ? [{ type: 'turn_started', turn_id: 'turn-1', thread_id: 'thread-1' }] : []),
      { type: 'response', content: 'partial reply', thread_id: 'thread-1' },
      { type: 'turn_replay_gap', turn_id: 'turn-1', thread_id: 'thread-1' },
      { type: 'response', content: 'must not mark this complete', thread_id: 'thread-1' }
    ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(wire)));
    const displayed: unknown[] = [];
    const started: string[] = [];
    const recovered: string[] = [];
    const recovering = await consumeTurnStream(
      new ChatApi().chatStream('hello', 'thread-1'),
      event => displayed.push(event),
      turnId => started.push(turnId),
      turnId => recovered.push(turnId)
    );
    expect(recovering).toBe(true);
    expect(recovered).toEqual(['turn-1']);
    expect(started).toEqual(hasStart ? ['turn-1'] : []);
    expect(displayed).toEqual([expect.objectContaining({ type: 'response', data: { content: 'partial reply', isComplete: false } })]);
  });

  it('finishes a complete turn without starting recovery', async () => {
    const wire = [
      { type: 'turn_started', turn_id: 'turn-2', thread_id: 'thread-2' },
      { type: 'response', content: 'complete reply', thread_id: 'thread-2' },
      { type: 'done', thread_id: 'thread-2' }
    ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(wire)));
    const displayed: string[] = [];
    const recovered: string[] = [];
    const recovering = await consumeTurnStream(
      new ChatApi().chatStream('hello', 'thread-2'),
      event => displayed.push(event.type), () => {}, turnId => recovered.push(turnId)
    );
    expect(recovering).toBe(false);
    expect(displayed).toEqual(['response', 'done']);
    expect(recovered).toEqual([]);
  });
});


it('keeps a dispatched partial and target link when replay is unavailable', async () => {
  const wire = [
    { type: 'dispatched', target_thread_id: 'target-thread', title: 'Target', thread_id: 'caller' },
    { type: 'response', content: 'partial target reply', thread_id: 'caller' },
    { type: 'turn_replay_gap', turn_id: 'target-turn', thread_id: 'caller' }
  ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(wire)));
  const displayed: Array<{ type: string; data: unknown }> = [];
  const recovered: string[] = [];
  const recovering = await consumeTurnStream(
    new ChatApi().chatStream('@target hello', 'caller'),
    event => displayed.push(event), () => {}, turnId => recovered.push(turnId)
  );
  expect(recovering).toBe(false);
  expect(recovered).toEqual([]);
  expect(displayed.map(event => event.type)).toEqual(['dispatched', 'response', 'error']);
  expect(displayed[0].data).toMatchObject({ threadId: 'target-thread', title: 'Target' });
  expect(displayed[1].data).toMatchObject({ content: 'partial target reply' });
  expect(displayed[2].data).toMatchObject({ code: 'turn_replay_gap', message: expect.stringContaining('linked thread') });
});
