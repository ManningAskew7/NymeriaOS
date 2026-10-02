import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// #440, the binding half of threadStateRefresh.test.ts: the deps the chat
// panel and Thread Settings call, bound to the REAL stores. A typed command
// or a panel write must land where the header chips and the status bar read
// (the chat store's stats and active model, the thread-config cache, the
// server settings), so a mis-bound dep fails here rather than leaving the
// chips stale. The api client is the network boundary.

const reg = vi.hoisted(() => ({
  hooks: [] as Array<() => void>,
  generation: 0,
}));

vi.mock('$lib/stores/config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
  identityReloadGeneration: () => reg.generation,
  scopedKey: (base: string) => base,
  currentIdentityScope: () => null,
  configStore: { identity: { id: 'default', role: 'admin' } },
}));
vi.mock('$lib/stores/errors.svelte', () => ({ errorsStore: { push: vi.fn() } }));
vi.mock('$lib/services/api.svelte', () => ({
  abortCurrentStream: vi.fn(),
  api: {
    executeCommand: vi.fn(),
    getThreadContextStats: vi.fn(),
    getThreadConfig: vi.fn(),
    getServerSettings: vi.fn(),
  },
}));
vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown, ctx: { action: string; resource: string }) =>
    `Could not ${ctx.action} ${ctx.resource}: ${(e as Error).message}`,
}));

import { refreshLiveThreadState, runLiveTypedCommand } from './liveThreadState';
import { api } from '$lib/services/api.svelte';
import { chatStore } from '$lib/stores/chat.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';
import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
import type { ContextStats, ThreadConfig } from '$lib/types';

function stats(threadId: string, model: string, usagePercentage: number): ContextStats {
  return {
    threadId,
    model,
    totalTokens: 1000,
    inputTokens: 900,
    outputTokens: 100,
    contextLimit: 200_000,
    usagePercentage,
    compactionCount: 0,
    lastCompaction: null,
    contextManagement: 'auto',
  } as ContextStats;
}

function config(threadId: string, model: string | null): ThreadConfig {
  return {
    threadId,
    llmConfig: model ? { model } : null,
    activeLlmFallback: null,
  } as unknown as ThreadConfig;
}

function settings(model: string) {
  return {
    llm_provider: 'anthropic',
    llm_provider_route: 'default',
    llm_model: model,
    llm_fast_model_resolved: null,
    llm_smart_model_resolved: null,
    memory_char_limit: null,
  };
}

let threadId = '';

beforeEach(() => {
  vi.clearAllMocks();
  for (const hook of reg.hooks) hook();
  chatStore.setContextStats(null);
  chatStore.setActiveModel('fallback-model');
  threadId = threadsStore.createThread('Scratch').id;
});

describe('the live thread-state deps reach the stores the chips read', () => {
  it('a typed command lands its card, the resolved model, the thread config and the global model', async () => {
    // The header already shows the old global default: a bare `/model`
    // must re-read it, not find the store loaded and skip.
    (api.getServerSettings as Mock).mockResolvedValueOnce(settings('global-old'));
    await serverSettingsStore.load();
    expect(serverSettingsStore.model).toBe('global-old');

    (api.executeCommand as Mock).mockResolvedValue({
      success: true,
      markdown: 'Model for this thread set to claude-x.',
      command: 'model',
      level: 'success',
    });
    (api.getThreadContextStats as Mock).mockResolvedValue(stats(threadId, 'claude-x', 12.5));
    (api.getThreadConfig as Mock).mockResolvedValue(config(threadId, 'claude-x'));
    (api.getServerSettings as Mock).mockResolvedValue(settings('global-y'));

    await runLiveTypedCommand('/model claude-x thread', threadId, '/model');

    expect(api.executeCommand).toHaveBeenCalledWith('/model claude-x thread', threadId);
    const card = chatStore.messages[chatStore.messages.length - 1];
    expect(card).toMatchObject({
      kind: 'command_result',
      commandInput: '/model claude-x thread',
      content: 'Model for this thread set to claude-x.',
      commandLevel: 'success',
    });
    expect(chatStore.activeModel).toBe('claude-x');
    expect(chatStore.contextStats?.usagePercentage).toBe(12.5);
    expect(threadConfigStore.getConfig(threadId)?.llmConfig?.model).toBe('claude-x');
    expect(serverSettingsStore.model).toBe('global-y');
  });

  it('a request failure is the humanized error card and nothing else', async () => {
    (api.executeCommand as Mock).mockRejectedValue(new TypeError('Failed to fetch'));

    await runLiveTypedCommand('/fast', threadId, '/fast');

    const card = chatStore.messages[chatStore.messages.length - 1];
    expect(card).toMatchObject({
      kind: 'command_result',
      content: 'Could not run the /fast command: Failed to fetch',
      status: 'error',
    });
    expect(api.getThreadContextStats).not.toHaveBeenCalled();
    expect(api.getServerSettings).not.toHaveBeenCalled();
    expect(chatStore.activeModel).toBe('fallback-model');
  });

  it('a connection switch mid-command lands no card and no refresh', async () => {
    let answer: (value: unknown) => void = () => undefined;
    (api.executeCommand as Mock).mockReturnValue(new Promise((resolve) => { answer = resolve; }));
    const before = chatStore.messages.length;

    const done = runLiveTypedCommand('/fallback revert', threadId, '/fallback');
    reg.generation += 1;
    answer({ success: true, markdown: 'Ended the fallback hold.', command: 'fallback revert', level: 'success' });
    await done;

    expect(chatStore.messages.length).toBe(before);
    expect(api.getThreadContextStats).not.toHaveBeenCalled();
  });

  it('a panel write (Revert, Save, Reset) refreshes the status bar for the open thread', async () => {
    (api.getThreadContextStats as Mock).mockResolvedValue(stats(threadId, 'claude-configured', 40));
    (api.getThreadConfig as Mock).mockResolvedValue(config(threadId, 'claude-configured'));
    (api.getServerSettings as Mock).mockResolvedValue(settings('global-y'));

    await refreshLiveThreadState(threadId);

    expect(api.getThreadContextStats).toHaveBeenCalledWith(threadId);
    expect(chatStore.activeModel).toBe('claude-configured');
    expect(threadConfigStore.getConfig(threadId)?.llmConfig?.model).toBe('claude-configured');
  });

  it('a panel write for a thread that is not open leaves the status bar alone', async () => {
    const other = threadsStore.createThread('Other').id;
    threadsStore.selectThread(other);
    (api.getThreadContextStats as Mock).mockResolvedValue(stats(threadId, 'claude-configured', 40));
    (api.getThreadConfig as Mock).mockResolvedValue(config(threadId, 'claude-configured'));
    (api.getServerSettings as Mock).mockResolvedValue(settings('global-y'));

    await refreshLiveThreadState(threadId);

    expect(chatStore.activeModel).toBe('fallback-model');
    expect(threadConfigStore.getConfig(threadId)?.llmConfig?.model).toBe('claude-configured');
  });
});
