import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync } from 'svelte';

// #242 review C-MED-1: the chat header's model chip, tool count, trigger
// count and global skills come from four global stores that a connection
// switch resets (their reload hooks drop the values and the loaded flags).
// The header's load effect must notice the reset and load the new backend's
// values; it used to track only `isConfigured`, so after a switch the badges
// stayed blank until Thread Settings happened to be opened.
//
// Delta review LOW-1: a switch nulls the identity and fires the hooks BEFORE
// the repoint, then fires them again once /me names the account. Loads run
// in between asked B with no account (three of the four need one), failed,
// and ran again after /me: the effect must wait for the identity.
//
// Real stores, a real effect (`$effect.root`) and the real config store
// driven through the switch's own sequence; the api client and GET /me are
// the network boundary.

const net = vi.hoisted(() => {
  const state = { hold: null as Promise<void> | null };
  (globalThis as { fetch?: unknown }).fetch = async () => {
    if (state.hold) await state.hold;
    return new Response(
      JSON.stringify({ id: 'default', email: 'default@localhost', display_name: 'Owner', role: 'admin' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  };
  return state;
});

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getDefaultTools: vi.fn(),
    getServerSettings: vi.fn(),
    getTriggers: vi.fn(),
    getGlobalSkills: vi.fn(),
  },
}));

import { keepHeaderStoresLoaded } from './headerStoreLoads.svelte';
import { configStore } from './config.svelte';
import { defaultToolsStore } from './defaultTools.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { triggersStore } from './triggers.svelte';
import { skillsStore } from './skills.svelte';
import { api } from '$lib/services/api.svelte';

const A = 'http://localhost:8097';
const B = 'http://localhost:8098';
const headerLoads = [api.getDefaultTools, api.getServerSettings, api.getTriggers, api.getGlobalSkills] as Mock[];

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

/**
 * The config half of a connection switch (`backendSwitch.svelte.ts`): reset
 * and park, repoint, then the forced /me. Returns once /me has answered.
 */
async function switchTo(url: string) {
  configStore.beginConnectionSwitch(url);
  configStore.apiUrl = url;
  configStore.apiKey = `nym_${url.slice(-4)}`;
  await configStore.refreshIdentity({ forceReload: true });
}

let stop: () => void = () => undefined;

beforeEach(async () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  net.hold = null;
  // Module-shared stores: every test starts signed in on A with them empty.
  await switchTo(A);
  vi.clearAllMocks();
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
    await switchTo(B);
    await settle();

    expect(serverSettingsStore.model).toBe('model-b');
    expect(defaultToolsStore.defaultToolNames).toEqual(['b_tool_1', 'b_tool_2']);
    expect(triggersStore.triggers.map((t) => t.id)).toEqual(['b-trigger']);
    expect(skillsStore.enabledGlobal).toEqual(['b-skill']);
  });

  it('asks B nothing while its /me is in flight, then each load exactly once (delta review LOW-1)', async () => {
    serve('a');
    stop = $effect.root(() => keepHeaderStoresLoaded());
    await settle();
    for (const load of headerLoads) load.mockClear();

    serve('b');
    let answer: () => void = () => undefined;
    net.hold = new Promise<void>((resolve) => {
      answer = resolve;
    });
    const switching = switchTo(B);
    await settle();

    // Reset, repointed at B, no account yet: nothing is asked, nothing shown.
    expect(configStore.apiUrl).toBe(B);
    expect(configStore.identity).toBeNull();
    for (const load of headerLoads) expect(load).not.toHaveBeenCalled();
    expect(serverSettingsStore.model).toBeNull();
    expect(defaultToolsStore.loaded).toBe(false);
    expect(triggersStore.loaded).toBe(false);
    expect(skillsStore.enabledGlobalLoaded).toBe(false);

    answer();
    await switching;
    await settle();

    for (const load of headerLoads) expect(load).toHaveBeenCalledTimes(1);
    expect(serverSettingsStore.model).toBe('model-b');
    expect(defaultToolsStore.defaultToolNames).toEqual(['b_tool_1', 'b_tool_2']);
    expect(triggersStore.triggers.map((t) => t.id)).toEqual(['b-trigger']);
    expect(skillsStore.enabledGlobal).toEqual(['b-skill']);
    expect(defaultToolsStore.error).toBeNull();
    expect(triggersStore.error).toBeNull();
    expect(skillsStore.enabledGlobalError).toBeNull();
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
