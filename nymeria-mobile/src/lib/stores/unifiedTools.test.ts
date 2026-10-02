import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// A per-tool toggle on mobile's Tools screen writes through the unified
// endpoint and must then re-read the list: the row's switch and its #164
// standard-tool badge render from this store, and the panel sends the
// opposite of the stored `enabled` on the next flip. Found live (It41): the
// re-read returned early on the setter's own loading flag, so a tool switched
// on still read off, kept its "new" badge, and a second flip sent "enable"
// again. Desktop's store already drops the flag first. The api is the network
// boundary.

const reg = vi.hoisted(() => ({ hooks: [] as Array<() => void> }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
  configStore: { identity: null },
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getUnifiedTools: vi.fn(),
    setUnifiedToolEnabled: vi.fn(),
    getDefaultTools: vi.fn(),
  },
}));
vi.mock('$lib/services/api/humanizeError', () => ({
  humanizeErrorText: (e: unknown) => `humanized: ${(e as Error).message}`,
}));

import { unifiedToolsStore } from './unifiedTools.svelte';
import { api } from '$lib/services/api.svelte';

const row = (enabled: boolean, coreStatus: string | null) => ({
  id: 'file_list',
  name: 'file_list',
  description: 'List a directory',
  category: 'general',
  toolType: 'builtin',
  enabled,
  coreStatus,
});
const listing = (enabled: boolean, coreStatus: string | null) => ({
  tools: [row(enabled, coreStatus)],
  builtinCount: 1,
  customCount: 0,
});

beforeEach(() => {
  // Reset, not clear: a queued once-value a red run left unconsumed must
  // not leak into the next test.
  vi.resetAllMocks();
  for (const hook of reg.hooks) hook();
});

describe('a per-tool toggle re-reads the list', () => {
  it('switching a new standard tool on, then off, follows the backend each time', async () => {
    (api.getUnifiedTools as Mock)
      .mockResolvedValueOnce(listing(false, 'new'))
      .mockResolvedValueOnce(listing(true, 'default'))
      .mockResolvedValueOnce(listing(false, 'declined'));
    (api.setUnifiedToolEnabled as Mock).mockResolvedValue(undefined);
    await unifiedToolsStore.loadTools();

    // The panel's handler: the opposite of what the store holds.
    const flip = () => unifiedToolsStore.setToolEnabled('file_list', !unifiedToolsStore.tools[0].enabled);

    expect(await flip()).toBe(true);
    expect(unifiedToolsStore.tools[0]).toMatchObject({ enabled: true, coreStatus: 'default' });
    expect(await flip()).toBe(true);

    expect((api.setUnifiedToolEnabled as Mock).mock.calls.map((c) => c[1])).toEqual([true, false]);
    expect(unifiedToolsStore.tools[0]).toMatchObject({ enabled: false, coreStatus: 'declined' });
    expect(unifiedToolsStore.loading).toBe(false);
  });

  it('a failed write leaves the list as it was and reports the error', async () => {
    (api.getUnifiedTools as Mock).mockResolvedValueOnce(listing(false, 'new'));
    (api.setUnifiedToolEnabled as Mock).mockRejectedValueOnce(new Error('boom'));
    await unifiedToolsStore.loadTools();

    expect(await unifiedToolsStore.setToolEnabled('file_list', true)).toBe(false);

    expect(api.getUnifiedTools).toHaveBeenCalledTimes(1);
    expect(unifiedToolsStore.tools[0]).toMatchObject({ enabled: false, coreStatus: 'new' });
    expect(unifiedToolsStore.error).toBe('humanized: boom');
    expect(unifiedToolsStore.loading).toBe(false);
  });
});
