import { describe, expect, it, vi } from 'vitest';

import type { CommandExecuteResponse, ContextStats } from '$lib/types';
import {
  refreshThreadState,
  runTypedCommand,
  type ThreadStateRefreshDeps,
  type TypedCommandDeps,
} from './threadStateRefresh';

// #440: after a typed command (or a Thread Settings write) the header chips
// and the status bar must show the thread's new state within one round
// trip, not at the next turn's end. The helper re-reads context stats, then
// the thread config, plus the server settings (a bare `/model` writes the
// GLOBAL default), and lands nothing on a thread or backend the user has
// left. Deps are injected, so the order and the guards are observable.

function stats(threadId: string, model: string): ContextStats {
  return {
    threadId,
    model,
    totalTokens: 1000,
    inputTokens: 900,
    outputTokens: 100,
    contextLimit: 200_000,
    usagePercentage: 0.5,
    compactionCount: 0,
    lastCompaction: null,
    contextManagement: 'auto',
  } as ContextStats;
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A fake world: the current thread, the connection generation, and a call log. */
function world(options: { current?: string | null; stats?: ContextStats | null } = {}) {
  const log: string[] = [];
  const state = {
    current: options.current === undefined ? 't-1' : options.current,
    generation: 7,
  };
  const applied: ContextStats[] = [];
  const cards: Array<{ line: string; markdown: string; success: boolean; level?: string }> = [];
  const deps: TypedCommandDeps = {
    getThreadContextStats: vi.fn(async (threadId: string) => {
      log.push(`stats:${threadId}`);
      return options.stats === undefined ? stats(threadId, 'claude-x') : options.stats;
    }),
    loadThreadConfig: vi.fn(async (threadId: string) => {
      log.push(`config:${threadId}`);
    }),
    refreshServerSettings: vi.fn(async () => {
      log.push('settings');
    }),
    currentThreadId: () => state.current,
    identityGeneration: () => state.generation,
    applyContextStats: (s: ContextStats) => {
      log.push(`apply:${s.threadId}:${s.model}`);
      applied.push(s);
    },
    executeCommand: vi.fn(async (line: string, threadId?: string): Promise<CommandExecuteResponse> => {
      log.push(`execute:${line}:${threadId}`);
      return { success: true, markdown: 'Model for this thread set to claude-x.', command: 'model', level: 'success' };
    }),
    addCommandResult: (line: string, markdown: string, success: boolean, level?: string) => {
      log.push(`card:${success}`);
      cards.push({ line, markdown, success, level });
    },
    describeFailure: (error: unknown, slashRoot: string) => `Could not run the ${slashRoot} command: ${(error as Error).message}`,
  };
  return { deps, log, state, applied, cards };
}

describe('refreshThreadState', () => {
  it('re-reads stats BEFORE the config, applies them to the open thread, and refreshes server settings', async () => {
    const { deps, log, applied } = world();
    const pendingStats = deferred<ContextStats | null>();
    deps.getThreadContextStats = vi.fn(() => {
      log.push('stats:t-1');
      return pendingStats.promise;
    });

    const done = refreshThreadState(deps, 't-1');
    await Promise.resolve();
    // The stats read runs the resolver, which evicts an expired idle hold;
    // a config read racing it could still show the dead hold.
    expect(log).not.toContain('config:t-1');

    pendingStats.resolve(stats('t-1', 'claude-x'));
    await done;

    expect(log.indexOf('stats:t-1')).toBeLessThan(log.indexOf('config:t-1'));
    expect(log).toContain('settings');
    expect(applied.map((s) => s.model)).toEqual(['claude-x']);
  });

  it('a thread switch before the stats land: the status bar keeps the new thread, the old config is still refreshed', async () => {
    const { deps, log, state, applied } = world();
    const pendingStats = deferred<ContextStats | null>();
    deps.getThreadContextStats = vi.fn(() => pendingStats.promise);

    const done = refreshThreadState(deps, 't-1');
    state.current = 't-2';
    pendingStats.resolve(stats('t-1', 'claude-x'));
    await done;

    expect(applied).toEqual([]);
    expect(log).toContain('config:t-1');
  });

  it('a connection switch before the stats land: nothing from the old backend lands, no config read', async () => {
    const { deps, log, state, applied } = world();
    const pendingStats = deferred<ContextStats | null>();
    deps.getThreadContextStats = vi.fn(() => pendingStats.promise);

    const done = refreshThreadState(deps, 't-1');
    state.generation += 1;
    pendingStats.resolve(stats('t-1', 'claude-x'));
    await done;

    expect(applied).toEqual([]);
    expect(log.filter((entry) => entry.startsWith('config:'))).toEqual([]);
  });

  it('a failed stats read still reloads the config', async () => {
    for (const failure of ['null', 'throw'] as const) {
      const { deps, log, applied } = world({ stats: null });
      if (failure === 'throw') {
        deps.getThreadContextStats = vi.fn(async () => {
          throw new TypeError('Failed to fetch');
        });
      }
      await refreshThreadState(deps, 't-1');
      expect(applied).toEqual([]);
      expect(log).toContain('config:t-1');
    }
  });

  it('never rejects: a failing config or settings read is swallowed', async () => {
    const { deps } = world();
    deps.loadThreadConfig = vi.fn(async () => {
      throw new Error('404');
    });
    deps.refreshServerSettings = vi.fn(async () => {
      throw new Error('500');
    });
    await expect(refreshThreadState(deps, 't-1')).resolves.toBeUndefined();
  });

  it('with no thread only the server settings are refreshed', async () => {
    const { deps, log } = world({ current: null });
    await refreshThreadState(deps, undefined);
    expect(log).toEqual(['settings']);
  });
});

describe('runTypedCommand', () => {
  it('adds the result card, THEN refreshes the thread the command was sent for', async () => {
    const { deps, log, cards, applied } = world();

    await runTypedCommand(deps, '/model claude-x thread', 't-1', '/model');

    expect(cards).toEqual([{
      line: '/model claude-x thread',
      markdown: 'Model for this thread set to claude-x.',
      success: true,
      level: 'success',
    }]);
    expect(log.slice(0, 2)).toEqual(['execute:/model claude-x thread:t-1', 'card:true']);
    expect(log.indexOf('card:true')).toBeLessThan(log.indexOf('stats:t-1'));
    expect(log).toContain('config:t-1');
    expect(log).toContain('settings');
    expect(applied.map((s) => s.model)).toEqual(['claude-x']);
  });

  it('a command that answered with a failure still refreshes (it may have written part of its change)', async () => {
    const { deps, log, cards } = world();
    deps.executeCommand = vi.fn(async () => ({
      success: false,
      markdown: 'Provider switch failed after saving the model.',
      command: 'provider switch',
      level: 'error' as const,
    }));

    await runTypedCommand(deps, '/provider switch p thread', 't-1', '/provider');

    expect(cards.map((c) => c.success)).toEqual([false]);
    expect(log).toContain('stats:t-1');
    expect(log).toContain('config:t-1');
  });

  it('a transport failure shows the error card and refreshes nothing', async () => {
    const { deps, log, cards } = world();
    deps.executeCommand = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });

    await runTypedCommand(deps, '/fast', 't-1', '/fast');

    expect(cards).toEqual([{
      line: '/fast',
      markdown: 'Could not run the /fast command: Failed to fetch',
      success: false,
      level: undefined,
    }]);
    expect(log).toEqual(['card:false']);
  });

  it('keys the refresh to the SENT thread when the user switched threads mid-command', async () => {
    const { deps, log, state, applied } = world();
    const pending = deferred<CommandExecuteResponse>();
    deps.executeCommand = vi.fn(() => pending.promise);

    const done = runTypedCommand(deps, '/smart', 't-1', '/smart');
    state.current = 't-2';
    pending.resolve({ success: true, markdown: 'Smart model on.', command: 'smart', level: 'success' });
    await done;

    expect(log).toContain('stats:t-1');
    expect(log).toContain('config:t-1');
    expect(log.some((entry) => entry.endsWith(':t-2'))).toBe(false);
    expect(applied).toEqual([]);
  });

  it('a connection switch mid-command lands neither the card nor a refresh on the new backend', async () => {
    for (const outcome of ['answer', 'throw'] as const) {
      const { deps, log, state, cards } = world();
      const pending = deferred<CommandExecuteResponse>();
      deps.executeCommand = vi.fn(() => pending.promise);

      const done = runTypedCommand(deps, '/fallback revert', 't-1', '/fallback');
      state.generation += 1;
      if (outcome === 'answer') {
        pending.resolve({ success: true, markdown: 'Ended the fallback hold.', command: 'fallback revert', level: 'success' });
      } else {
        pending.reject(new TypeError('Failed to fetch'));
      }
      await done;

      expect(cards).toEqual([]);
      expect(log).toEqual([]);
    }
  });
});
