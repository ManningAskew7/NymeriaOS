import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SkillMetadata } from '$lib/types';

const mocks = vi.hoisted(() => ({
  getGlobalSkills: vi.fn(),
  installSkill: vi.fn(),
  listSkills: vi.fn(),
  registerIdentityReloadHook: vi.fn(),
  searchSkillsMarketplace: vi.fn(),
  setGlobalSkills: vi.fn(),
  uninstallSkill: vi.fn(),
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getGlobalSkills: mocks.getGlobalSkills,
    installSkill: mocks.installSkill,
    listSkills: mocks.listSkills,
    searchSkillsMarketplace: mocks.searchSkillsMarketplace,
    setGlobalSkills: mocks.setGlobalSkills,
    uninstallSkill: mocks.uninstallSkill,
  },
}));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: mocks.registerIdentityReloadHook,
}));

import { createSkillsStore } from './skills.svelte';

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function skill(name: string): SkillMetadata {
  return {
    name,
    description: `${name} description`,
    scope: 'global',
    allowed_tools: [],
    required_tools: [],
    tool_ttl: '2h',
    is_skill_kit: false,
    default_active: false,
    has_scripts: false,
    has_references: false,
    has_assets: false,
  };
}

async function waitFor(condition: () => boolean): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  expect(condition()).toBe(true);
}

describe('skillsStore refreshes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('queues a forced installed-skill refresh while a load is already in flight', async () => {
    const first = deferred<SkillMetadata[]>();
    const second = deferred<SkillMetadata[]>();
    mocks.listSkills
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const store = createSkillsStore();
    const initialLoad = store.loadInstalled();

    void store.refreshInstalled();

    expect(mocks.listSkills).toHaveBeenCalledTimes(1);

    first.resolve([skill('old-kit')]);
    await initialLoad;

    await waitFor(() => mocks.listSkills.mock.calls.length === 2);

    second.resolve([skill('credential-management')]);
    await waitFor(() => store.installed.some((s) => s.name === 'credential-management'));

    expect(store.installed.map((s) => s.name)).toEqual(['credential-management']);
  });

  it('queues a forced global-skill refresh while a load is already in flight', async () => {
    const first = deferred<string[]>();
    const second = deferred<string[]>();
    mocks.getGlobalSkills
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const store = createSkillsStore();
    const initialLoad = store.loadGlobal();

    void store.refreshGlobal();

    expect(mocks.getGlobalSkills).toHaveBeenCalledTimes(1);

    first.resolve(['old-kit']);
    await initialLoad;

    await waitFor(() => mocks.getGlobalSkills.mock.calls.length === 2);

    second.resolve(['credential-management']);
    await waitFor(() => store.enabledGlobal.includes('credential-management'));

    expect(store.enabledGlobal).toEqual(['credential-management']);
  });
});
