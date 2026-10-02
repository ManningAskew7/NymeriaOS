import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync } from 'svelte';
import type { ThreadConfig } from '$lib/types';

// Mobile ThreadSettingsPanel keeps a local snapshot of the thread config and
// an effect that follows the store while the panel is open (so a background
// reload surfaces the fallback-hold row live). The store holds RAW objects
// (its Map is not proxied); a deep `$state` snapshot proxies whatever it is
// given, so `cfg !== threadConfig` stayed true forever and the effect wrote
// itself into `effect_update_depth_exceeded`: the panel sat on "Loading
// thread settings..." on every open (found live in It35's closer leg). The
// snapshot is `$state.raw` now. This is the panel's effect in its exact
// shape over the real store (the api client is the network boundary); the
// component itself is not mountable here (no DOM, #446).

const reg = vi.hoisted(() => ({ hooks: [] as Array<() => void> }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: { getThreadConfig: vi.fn() },
}));

import { threadConfigStore } from './threadConfig.svelte';
import { api } from '$lib/services/api.svelte';

const getThreadConfig = api.getThreadConfig as Mock;

function config(instructions: string): ThreadConfig {
  return { threadId: 't1', instructions } as unknown as ThreadConfig;
}

let stop: () => void = () => undefined;

beforeEach(() => {
  for (const hook of reg.hooks) hook();
  getThreadConfig.mockReset();
});

afterEach(() => {
  stop();
  stop = () => undefined;
});

describe('the Thread Settings snapshot follows the store without looping', () => {
  it('settles once it holds the store config, then follows a reload', async () => {
    getThreadConfig.mockResolvedValue(config('first'));
    let threadConfig = $state.raw<ThreadConfig | null>(null);
    const open = true;
    let runs = 0;

    stop = $effect.root(() => {
      $effect(() => {
        runs += 1;
        const cfg = threadConfigStore.configs.get('t1');
        if (open && cfg && cfg !== threadConfig) {
          threadConfig = cfg;
        }
      });
    });
    flushSync();

    await threadConfigStore.loadConfig('t1');
    flushSync();
    expect(threadConfig).toBe(threadConfigStore.getConfig('t1'));
    expect(threadConfig?.instructions).toBe('first');
    expect(runs).toBeLessThanOrEqual(3);

    // A background reload (ChatPanel's turn end) lands a new object.
    getThreadConfig.mockResolvedValue(config('second'));
    await threadConfigStore.loadConfig('t1');
    flushSync();
    expect(threadConfig?.instructions).toBe('second');
    expect(runs).toBeLessThanOrEqual(5);
  });
});
