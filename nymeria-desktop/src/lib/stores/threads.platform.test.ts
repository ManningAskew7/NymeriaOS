import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    listThreadsWithMetadata: vi.fn(),
    listThreadTeams: vi.fn().mockResolvedValue([]),
  },
}));

import { threadsStore } from './threads.svelte';

function backendRow(overrides: Record<string, unknown>) {
  return {
    thread_id: 'x',
    title: 'X',
    pinned: false,
    platform: 'desktop',
    platform_meta: null,
    created_at: null,
    updated_at: null,
    title_source: 'default',
    ...overrides,
  };
}

describe('platform (shared-channel) threads in the list', () => {
  it('keeps the backend shared flag and the platform so a Twitch chat lists as one', () => {
    threadsStore._applyBackendThreads([
      backendRow({ thread_id: 'personal', title: 'Mine' }),
      backendRow({
        thread_id: 'twitch_chatter',
        title: 'Twitch chatter: jamesoakwood in #redopz',
        pinned: true,
        platform: 'twitch',
        shared: true,
        title_source: 'user',
      }),
      backendRow({ thread_id: 'twitch_silk', title: 'twitch_silk', platform: 'twitch', shared: true }),
    ]);

    const byId = new Map(threadsStore.threads.map((t) => [t.id, t]));
    expect(byId.get('personal')?.shared).toBe(false);
    expect(byId.get('twitch_chatter')).toMatchObject({
      shared: true,
      pinned: true,
      platform: 'twitch',
      title: 'Twitch chatter: jamesoakwood in #redopz',
    });
    // A never-titled channel thread keeps the id the backend sent as its
    // title instead of collapsing to the "New Thread" placeholder.
    expect(byId.get('twitch_silk')?.title).toBe('twitch_silk');
  });
});
