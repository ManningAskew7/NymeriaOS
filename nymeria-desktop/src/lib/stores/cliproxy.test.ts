import { describe, expect, it, vi, beforeEach } from 'vitest';

// The store reaches the backend through the shared api singleton and the
// Tauri runtime through a dynamic import; mock both so refresh() populates
// state without a backend (the Tauri probe failing is the normal web path).
vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getCLIProxyStatus: vi.fn(),
    listCLIProxyAuthFiles: vi.fn(),
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
