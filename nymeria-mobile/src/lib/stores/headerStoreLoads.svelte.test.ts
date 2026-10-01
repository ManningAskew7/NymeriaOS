import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync } from 'svelte';

// #242 review C-MED-1: the chat header's model chip, tool count, trigger
// count and global skills come from four global stores that a connection
// switch resets (their reload hooks drop the values and the loaded flags).
// The header's load effect must notice the reset and load the new backend's
// values; it used to track only `isConfigured`, so after a switch the badges
// stayed blank until Thread Settings happened to be opened. Real stores and a
// real effect (`$effect.root`); the api client is the network boundary and
// the config module's hook registry is captured so the test can fire a switch.

const reg = vi.hoisted(() => ({ hooks: [] as Array<() => void> }));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: (hook: () => void) => {
    reg.hooks.push(hook);
    return () => undefined;
  },
  configStore: {
    isConfigured: true,
    identity: { id: 'default', role: 'admin' },
  },
}));
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getDefaultTools: vi.fn(),
    getServerSettings: vi.fn(),
    getTriggers: vi.fn(),
    getGlobalSkills: vi.fn(),
  },
}));

import { keepHeaderStoresLoaded } from './headerStoreLoads.svelte';
import { defaultToolsStore } from './defaultTools.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { triggersStore } from './triggers.svelte';
import { skillsStore } from './skills.svelte';
import { api } from '$lib/services/api.svelte';

/** One backend's answers to the four header loads. */
function serve(tag: string) {
  (api.getDefaultTools as Mock).mockResolvedValue({
    available_tools: [],
    default_tools: [`${tag}_tool_1`, `${tag}_tool_2`],
    callable_thread_count: 0,
  });
  (api.getServerSettings as Mock).mockResolvedValue({
    llm_provider: 'anthropic',
    llm_provider_route: 'default',
    llm_model: `model-${tag}`,
    llm_fast_model_resolved: null,
    llm_smart_model_resolved: null,
    memory_char_limit: null,
  });
  (api.getTriggers as Mock).mockResolvedValue([{ id: `${tag}-trigger`, enabled: true, thread_id: 't' }]);
  (api.getGlobalSkills as Mock).mockResolvedValue([`${tag}-skill`]);
}

/** Run pending effects, then let the loads they started resolve. */
async function settle() {
  flushSync();
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
  flushSync();
}

/** What a connection switch does to these stores: every reload hook fires. */
function switchBackend() {
  for (const hook of reg.hooks) hook();
}

let stop: () => void = () => undefined;

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  switchBackend(); // module-shared stores: start each test empty
});

afterEach(() => {
  stop();
  vi.restoreAllMocks();
});

describe('the chat header keeps its stores loaded across a connection switch', () => {
  it('loads backend A`s values, then backend B`s after a switch, without any panel opening', async () => {
    serve('a');
    stop = $effect.root(() => keepHeaderStoresLoaded());
    await settle();
    expect(serverSettingsStore.model).toBe('model-a');
    expect(defaultToolsStore.defaultToolNames).toEqual(['a_tool_1', 'a_tool_2']);
    expect(triggersStore.triggers.map((t) => t.id)).toEqual(['a-trigger']);
    expect(skillsStore.enabledGlobal).toEqual(['a-skill']);

    serve('b');
    switchBackend();
    await settle();

    expect(serverSettingsStore.model).toBe('model-b');
    expect(defaultToolsStore.defaultToolNames).toEqual(['b_tool_1', 'b_tool_2']);
    expect(triggersStore.triggers.map((t) => t.id)).toEqual(['b-trigger']);
    expect(skillsStore.enabledGlobal).toEqual(['b-skill']);
  });

  it('a failed load latches and is not re-driven, so a refusing backend is asked once per switch', async () => {
    (api.getDefaultTools as Mock).mockRejectedValue(new Error('down'));
    (api.getServerSettings as Mock).mockRejectedValue(new Error('down'));
    (api.getTriggers as Mock).mockRejectedValue(new Error('down'));
    (api.getGlobalSkills as Mock).mockRejectedValue(new Error('down'));
    stop = $effect.root(() => keepHeaderStoresLoaded());
    await settle();
    await settle();

    expect(api.getServerSettings).toHaveBeenCalledTimes(1);
    expect(api.getDefaultTools).toHaveBeenCalledTimes(1);
    expect(api.getTriggers).toHaveBeenCalledTimes(1);
    expect(api.getGlobalSkills).toHaveBeenCalledTimes(1);
    expect(serverSettingsStore.model).toBeNull();
  });
});
