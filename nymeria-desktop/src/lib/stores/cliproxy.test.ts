import { describe, expect, it, vi, beforeEach } from 'vitest';

// The store reaches the backend through the shared api singleton and the
// Tauri runtime through a dynamic import; mock both so refresh() populates
// state without a backend (the Tauri probe failing is the normal web path).
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getCLIProxyStatus: vi.fn(),
    listCLIProxyAuthFiles: vi.fn(),
    applyCLIProxyRoute: vi.fn(),
  },
}));
vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn().mockRejectedValue(new Error('no tauri runtime')),
}));

import { cliproxyStore } from './cliproxy.svelte';
import { api } from '$lib/services/api.svelte';
import type { CLIProxyProviderInfo } from '$lib/types';

const GEMINI_PROVIDER: CLIProxyProviderInfo = {
  id: 'gemini-cli',
  label: 'Gemini CLI (Google account, legacy)',
  description: '',
  flow: 'browser',
  nymeria_provider: 'openai',
  url_shape: 'v1',
  api_mode: 'chat_completions',
  key_env_var: 'OPENAI_API_KEY',
  default_model: 'gemini-3-pro-preview',
  tos_warning: '',
  auth_file_provider: 'gemini',
  auth_file_providers: ['gemini', 'gemini-cli'],
  supported: true,
  logged_in: true,
};

const CLAUDE_PROVIDER: CLIProxyProviderInfo = {
  id: 'claude',
  label: 'Claude (Max/Pro subscription)',
  description: '',
  flow: 'browser',
  nymeria_provider: 'anthropic',
  url_shape: 'root',
  api_mode: '',
  key_env_var: 'ANTHROPIC_API_KEY',
  default_model: 'claude-opus-5',
  tos_warning: '',
  auth_file_provider: 'claude',
  auth_file_providers: ['claude'],
  supported: true,
  logged_in: true,
};

// The listing spelling is proxy-version-dependent: v7 binaries report the
// target id ("gemini-cli") where older ones (and the catalog field) say
// "gemini". Filtering on only one spelling made a completed Gemini login
// invisible (backlog #101 entry 30).
const AUTH_FILES = [
  { name: 'gemini-v7.json', provider: 'gemini-cli' },
  { name: 'gemini-v6.json', provider: 'gemini' },
  { name: 'gemini-file-shaped.json', type: 'gemini' },
  { name: 'claude-a.json', provider: 'claude' },
];

async function refreshWith(providers: CLIProxyProviderInfo[]) {
  vi.mocked(api.getCLIProxyStatus).mockResolvedValue({
    configured: true,
    reachable: true,
    management_html_url: null,
    detail: '',
    providers,
  });
  vi.mocked(api.listCLIProxyAuthFiles).mockResolvedValue(
    AUTH_FILES.map((file) => ({ ...file }))
  );
  await cliproxyStore.refresh();
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('authFilesFor', () => {
  it('accepts every backend-listed spelling and the type fallback', async () => {
    await refreshWith([GEMINI_PROVIDER, CLAUDE_PROVIDER]);
    expect(
      cliproxyStore.authFilesFor('gemini-cli').map((file) => file.name)
    ).toEqual(['gemini-v7.json', 'gemini-v6.json', 'gemini-file-shaped.json']);
  });

  it('never crosses providers', async () => {
    await refreshWith([GEMINI_PROVIDER, CLAUDE_PROVIDER]);
    expect(
      cliproxyStore.authFilesFor('claude').map((file) => file.name)
    ).toEqual(['claude-a.json']);
  });

  it('falls back to the local union on backends without auth_file_providers', async () => {
    const legacy = { ...GEMINI_PROVIDER };
    delete legacy.auth_file_providers;
    await refreshWith([legacy, CLAUDE_PROVIDER]);
    expect(
      cliproxyStore.authFilesFor('gemini-cli').map((file) => file.name)
    ).toEqual(['gemini-v7.json', 'gemini-v6.json', 'gemini-file-shaped.json']);
  });
});

// The per-thread CLIProxy walkthrough (thread Settings > Model) applies
// through the same store call as the global Proxy tab; the scope is the
// only thing that keeps "Use for this thread" from rewriting every chat.
describe('applyRoute', () => {
  const APPLIED_THREAD = {
    scope: 'thread' as const,
    provider: 'openai',
    model: 'gpt-6-astra',
    base_url: 'http://cli-proxy-api:8317/v1',
    api_mode: 'responses',
    thread_id: 'twitch_silk',
    restart_required: false,
  };

  it('sends scope thread with the thread id and reports a thread-scoped message', async () => {
    vi.mocked(api.applyCLIProxyRoute).mockResolvedValue(APPLIED_THREAD);
    const applied = await cliproxyStore.applyRoute('codex', {
      model: 'gpt-6-astra',
      scope: 'thread',
      threadId: 'twitch_silk',
    });
    expect(api.applyCLIProxyRoute).toHaveBeenCalledWith({
      provider: 'codex',
      model: 'gpt-6-astra',
      scope: 'thread',
      thread_id: 'twitch_silk',
      gatekeeper_key: undefined,
    });
    expect(applied).toEqual(APPLIED_THREAD);
    expect(cliproxyStore.message).toBe('Thread routed through CLIProxy (gpt-6-astra).');
    expect(cliproxyStore.error).toBeNull();
  });

  it('defaults to global scope with no thread id when none is given', async () => {
    vi.mocked(api.applyCLIProxyRoute).mockResolvedValue({
      ...APPLIED_THREAD,
      scope: 'global',
      thread_id: null,
    });
    await cliproxyStore.applyRoute('codex', { model: 'gpt-6-astra' });
    expect(api.applyCLIProxyRoute).toHaveBeenCalledWith(
      expect.objectContaining({ provider: 'codex', scope: 'global', thread_id: undefined })
    );
    expect(cliproxyStore.message).toBe('Backend route set to openai via CLIProxy (gpt-6-astra).');
  });

  it('clearNotices drops a previous scope\'s banner so a fresh panel mount starts clean', async () => {
    vi.mocked(api.applyCLIProxyRoute).mockResolvedValue({ ...APPLIED_THREAD, scope: 'global', thread_id: null });
    await cliproxyStore.applyRoute('codex', { model: 'gpt-6-astra' });
    expect(cliproxyStore.message).not.toBeNull();
    cliproxyStore.clearNotices();
    expect(cliproxyStore.message).toBeNull();
    expect(cliproxyStore.error).toBeNull();
  });

  it('surfaces a failed apply as an error, returns null, and leaves no success message', async () => {
    vi.mocked(api.applyCLIProxyRoute).mockRejectedValue(
      new Error('CLIPROXY_MANAGEMENT_URL is not set; configure the proxy before applying a route')
    );
    const applied = await cliproxyStore.applyRoute('codex', {
      scope: 'thread',
      threadId: 'twitch_silk',
    });
    expect(applied).toBeNull();
    expect(cliproxyStore.message).toBeNull();
    expect(cliproxyStore.error).toContain('CLIPROXY_MANAGEMENT_URL is not set');
  });
});
