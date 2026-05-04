import type { LLMProvider } from '$lib/types';

export type SettingsDisplayProvider =
  | 'anthropic_proxy'
  | 'anthropic_direct'
  | 'openai'
  | 'openai_custom'
  | 'openrouter'
  | 'local_openai';

export type ThreadDisplayProvider =
  | ''
  | 'anthropic_proxy'
  | 'anthropic_direct'
  | 'openai'
  | 'openrouter'
  | 'openai_custom';

export const LOCAL_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', 'host.docker.internal'];
export const DEFAULT_LOCAL_BASE_URL = 'http://host.docker.internal:8080/v1';
export const DEFAULT_CLIPROXY_BASE_URL = 'http://cli-proxy-api:8317';
export const DEFAULT_OPENAI_CLIPROXY_BASE_URL = 'http://cli-proxy-api-latest:8317/v1';
export const DEFAULT_CUSTOM_OPENAI_BASE_URL = DEFAULT_OPENAI_CLIPROXY_BASE_URL;

const MANAGED_BASE_URLS = [
  DEFAULT_CLIPROXY_BASE_URL,
  DEFAULT_OPENAI_CLIPROXY_BASE_URL,
  DEFAULT_LOCAL_BASE_URL,
];

export function isLocalBaseUrl(baseUrl: string | null | undefined): boolean {
  if (!baseUrl) return false;
  return LOCAL_HOSTS.some((host) => baseUrl.includes(host));
}

export function normalizeBaseUrl(baseUrl: string | null | undefined): string {
  return (baseUrl || '').trim().replace(/\/+$/, '');
}

export function isManagedBaseUrl(baseUrl: string | null | undefined): boolean {
  const normalized = normalizeBaseUrl(baseUrl);
  return !!normalized && MANAGED_BASE_URLS.some((url) => normalizeBaseUrl(url) === normalized);
}

export function toSettingsDisplayProvider(
  provider: LLMProvider,
  baseUrl: string | null | undefined
): SettingsDisplayProvider {
  if (provider === 'anthropic' && !baseUrl) return 'anthropic_direct';
  if (provider === 'anthropic') return 'anthropic_proxy';
  if (provider === 'openai' && isLocalBaseUrl(baseUrl)) return 'local_openai';
  if (provider === 'openai' && baseUrl) return 'openai_custom';
  return provider as SettingsDisplayProvider;
}

export function fromSettingsDisplayProvider(
  displayProvider: SettingsDisplayProvider
): { provider: LLMProvider; clearBaseUrl: boolean } {
  if (displayProvider === 'anthropic_proxy') return { provider: 'anthropic', clearBaseUrl: false };
  if (displayProvider === 'anthropic_direct') return { provider: 'anthropic', clearBaseUrl: true };
  if (displayProvider === 'local_openai') return { provider: 'openai', clearBaseUrl: false };
  if (displayProvider === 'openai_custom') return { provider: 'openai', clearBaseUrl: false };
  if (displayProvider === 'openai') return { provider: 'openai', clearBaseUrl: true };
  return { provider: displayProvider as LLMProvider, clearBaseUrl: false };
}

export function toThreadDisplayProvider(
  provider: string,
  baseUrl?: string | null
): ThreadDisplayProvider {
  if (!provider) return '';
  if (provider === 'anthropic' && baseUrl === '') return 'anthropic_direct';
  if (provider === 'anthropic') return 'anthropic_proxy';
  if (provider === 'openai' && baseUrl) return 'openai_custom';
  return provider as ThreadDisplayProvider;
}

export function fromThreadDisplayProvider(
  displayProvider: ThreadDisplayProvider
): { provider: string; baseUrl: string | null } {
  if (displayProvider === '') return { provider: '', baseUrl: null };
  if (displayProvider === 'anthropic_proxy') return { provider: 'anthropic', baseUrl: null };
  if (displayProvider === 'anthropic_direct') return { provider: 'anthropic', baseUrl: '' };
  if (displayProvider === 'openai_custom') {
    return { provider: 'openai', baseUrl: DEFAULT_CUSTOM_OPENAI_BASE_URL };
  }
  return { provider: displayProvider, baseUrl: null };
}

export function supportsOpenAiApiMode(provider: string): boolean {
  return provider === 'openai' || provider === 'openrouter';
}
