import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/config.svelte', () => ({ configStore: {} }));
vi.mock('./credentials', () => ({
  CredentialsApi: class {
    getBaseUrl() { return 'http://test'; }
    getHeaders() { return {}; }
  }
}));

import { ChatApi, consumeTurnReplay, consumeTurnStream } from './chat';

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


describe('queue stream promotion', () => {
  it('exposes a promoted holder and its whole response, including a final line without newline', async () => {
    const wire = [
      { type: 'queued', content: 'Waiting' },
      { type: 'dispatched', target_thread_id: 'child', title: 'Child', thread_id: 'thread' },
      { type: 'turn_started', turn_id: 'promoted', thread_id: 'thread' },
      { type: 'tool_call', id: 'tc', name: 'lookup', args: {} },
      { type: 'response', content: 'promoted answer', thread_id: 'thread' },
      { type: 'done', thread_id: 'thread' }
    ].map(event => `data: ${JSON.stringify(event)}`).join('\n\n');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(wire)));
    const events = [];
    for await (const event of new ChatApi().queuePromptStream('next', 'thread', new AbortController())) {
      events.push(event);
    }
    expect(events.map(event => event.type)).toEqual(['queued', 'turn_started', 'dispatched', 'tool_call', 'response', 'done']);
    expect(events.find(event => event.type === 'response')?.data).toMatchObject({ content: 'promoted answer' });
    expect(events.find(event => event.type === 'dispatched')?.data).toMatchObject({ threadId: 'child' });
  });

  it('retains a queue receipt ID and suppresses holder fanout before promotion', async () => {
    const wire = [
      { type: 'prompt_queued', prompt_id: 'server-prompt', position: 2, source: 'user' },
      { type: 'response', content: 'holder response must render only once' },
      { type: 'prompt_absorbed' }
    ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(wire)));
    const events = [];
    for await (const event of new ChatApi().queuePromptStream('next', 'thread', new AbortController())) events.push(event);
    expect(events.map(event => event.type)).toEqual(['prompt_queued', 'prompt_absorbed']);
    expect(events[0].data).toMatchObject({ promptId: 'server-prompt', position: 2 });
  });
});

vi.mock('$lib/stores/threadConfig.svelte', () => ({ threadConfigStore: { getConfig: () => null } }));
import { ThreadsApi } from './threads';
import { adoptCurrentStream, abortCurrentStream, hasActiveStreamForThread } from './chat';

it.each([
  [200, { withdrawn: true }, true],
  [404, { detail: { code: 'prompt_not_queued' } }, true],
  [404, { detail: 'Thread not found' }, false],
  [503, { detail: 'Service unavailable' }, false]
])('withdrawal handles status %s without hiding other failures', async (status, body, success) => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: Number(status) }));
  vi.stubGlobal('fetch', fetchMock);
  const result = new ChatApi().withdrawQueuedPrompt('thread/one', 'prompt/id');
  if (success) await expect(result).resolves.toBeUndefined();
  else await expect(result).rejects.toThrow();
  expect(fetchMock).toHaveBeenCalledWith('http://test/threads/thread%2Fone/queue/prompt%2Fid',
    expect.objectContaining({ method: 'DELETE' }));
});

it('Stop keeps the adopted controller after a predecessor closes', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('data: {"type":"turn_started","turn_id":"old"}\n\n')));
  const old = new ChatApi().chatStream('original', 'thread');
  await old.next();
  const promoted = new AbortController();
  adoptCurrentStream(promoted, 'thread');
  await old.return(undefined);
  expect(hasActiveStreamForThread('thread')).toBe(true);
  expect(promoted.signal.aborted).toBe(false);
  abortCurrentStream();
  expect(promoted.signal.aborted).toBe(true);
  expect(hasActiveStreamForThread('thread')).toBe(false);
});

it('maps live and saved batch inputs to identical source-marked content', async () => {
  const inputs = [
    { prompt_id: 'p1', message_id: 'graph-1', position: 1, total: 2, source: 'user', source_label: 'Manning', user_id: 'owner', enqueued_at: 1, text: 'first', model_content: '[Time: first]\n[Trigger: User | queued request 1/2 | source: Manning]\n\nfirst' },
    { prompt_id: 'p2', message_id: 'graph-2', position: 2, total: 2, source: 'callable', source_label: 'Research Agent', user_id: 'owner', enqueued_at: 2, text: 'second', model_content: '[Time: second]\n[Trigger: Callable | queued request 2/2 | source: Research Agent]\n\nsecond' }
  ];
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(new Response(`data: ${JSON.stringify({ type: 'prompt_injected', batch_id: 'b1', count: 2, prompt_ids: ['p1', 'p2'], prompts: inputs })}\n\n`))
    .mockResolvedValueOnce(new Response(JSON.stringify({ messages: [
      { role: 'system', kind: 'queued_batch', content: 'Batched inputs', queued_batch: { id: 'b1', total: 2, inputs } },
      { role: 'assistant', content: 'Shared reply' }
    ] }))));
  const stream = new ChatApi().chatStream('start', 'thread');
  const live = (await stream.next()).value as import('$lib/types').SSEEvent;
  await stream.return(undefined);
  const history = await new ThreadsApi().getThreadHistory('thread');
  const saved = history.messages[0];
  expect(saved.kind).toBe('queued_batch');
  expect(saved.queuedBatch).toEqual((live.data as { queuedBatch: unknown }).queuedBatch);
  expect(saved.queuedBatch?.inputs.map(input => [input.source, input.sourceLabel, input.modelContent]))
    .toEqual(inputs.map(input => [input.source, input.source_label, input.model_content]));
  expect(saved.queuedBatch?.inputs.map(input => input.messageId)).toEqual(['graph-1', 'graph-2']);
  expect(saved.graphMessageId).toBeUndefined();
  expect(history.messages[1].content).toBe('Shared reply');
});


it.each([false, true])('promotes an initial replay gap and preserves dispatch routing (%s)', async dispatched => {
  const wire = [
    ...(dispatched ? [{ type: 'dispatched', target_thread_id: 'target', title: 'Target', thread_id: 'caller' }] : []),
    { type: 'turn_replay_gap', turn_id: 'holder', thread_id: 'caller' }
  ].map(e => `data: ${JSON.stringify(e)}\n\n`).join('');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(wire)));
  const events = [];
  for await (const event of new ChatApi().queuePromptStream('next', 'caller', new AbortController())) events.push(event);
  expect(events.map(e => e.type)).toEqual(dispatched ? ['dispatched', 'turn_replay_gap'] : ['turn_replay_gap']);
  expect(events.at(-1)?.data).toMatchObject({ turnId: 'holder' });
});


it.each(['provider_error', 'cancelled'])('keeps a replayed %s and partial output instead of reconciling it away', async code => {
  const rendered: unknown[] = [];
  let attached = false;
  async function* replay(): AsyncGenerator<import('$lib/types').SSEEvent> {
    yield { type: 'turn_attach', data: {}, timestamp: new Date() };
    yield { type: 'response', data: { content: 'partial reply' }, timestamp: new Date() };
    yield { type: 'error', data: { code, message: 'visible terminal reason' }, timestamp: new Date() };
    yield { type: 'done', data: {}, timestamp: new Date() };
  }
  const outcome = await consumeTurnReplay(replay(), event => rendered.push(event.data),
    () => { attached = true; }, () => true);
  expect(attached).toBe(true);
  expect(outcome).toBe('failed');
  expect(rendered).toEqual([{ content: 'partial reply' }, { code, message: 'visible terminal reason' }, {}]);
});

it.each([
  ['turn_not_found', 'reconcile'], ['reattach_failed', 'retry']
])('preserves recovery control %s without rendering it as a model error', async (code, expected) => {
  async function* replay(): AsyncGenerator<import('$lib/types').SSEEvent> {
    yield { type: 'error', data: { code }, timestamp: new Date() };
  }
  const rendered: unknown[] = [];
  const outcome = await consumeTurnReplay(replay(), event => rendered.push(event), () => {}, () => true);
  expect(outcome).toBe(expected);
  expect(rendered).toEqual([]);
});


it('preserves a received replay error when the transport subsequently fails', async () => {
  async function* replay(): AsyncGenerator<import('$lib/types').SSEEvent> {
    yield { type: 'error', data: { code: 'provider_error', message: 'Provider rejected request' }, timestamp: new Date() };
    throw new TypeError('network lost after terminal error');
  }
  const rendered: unknown[] = [];
  const outcome = await consumeTurnReplay(replay(), event => rendered.push(event.data), () => {}, () => true);
  expect(outcome).toBe('failed');
  expect(rendered).toEqual([{ code: 'provider_error', message: 'Provider rejected request' }]);
});
