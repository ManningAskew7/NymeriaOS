import type { LLMProvider, LLMProviderSpec, ProviderTier } from '$lib/types';
import type { ProviderSelectGroup, ProviderSelectOption } from '$lib/components/common/ProviderSelect.svelte';

export type ProviderOption = { value: string; label: string };
export type SettingsDisplayProvider = string;
export type ThreadDisplayProvider = string;

// Maps each synthetic display value to (backend provider id it resolves to,
// short descriptive sub-line for the picker row).
const SYNTHETIC_DISPLAY_PROVIDERS: Record<string, { backend: string; description: string }> = {
  anthropic_proxy: {
    backend: 'anthropic',
    description: 'Anthropic via CLIProxy (Claude Pro/Max subscription).',
  },
  anthropic_direct: {
    backend: 'anthropic',
    description: 'Direct Anthropic API (pay-per-token, needs ANTHROPIC_API_KEY).',
  },
  openai_custom: {
    backend: 'openai',
    description: 'OpenAI client with a custom base URL (e.g. CLIProxy Codex OAuth).',
  },
  local_openai: {
    backend: 'openai',
    description: 'Local OpenAI-compatible server (llama.cpp, LM Studio, vLLM, ...).',
  },
};

function findSpec(catalog: LLMProviderSpec[], id: string): LLMProviderSpec | undefined {
  const target = id.trim().toLowerCase();
  return catalog.find((s) => s.id === target || (s.aliases ?? []).includes(target));
}

function tierForOption(catalog: LLMProviderSpec[], value: string): ProviderTier {
  const synthetic = SYNTHETIC_DISPLAY_PROVIDERS[value];
  const lookupId = synthetic ? synthetic.backend : value;
  const spec = findSpec(catalog, lookupId);
  return spec?.tier ?? 'unverified';
}

function enrichOption(
  option: ProviderOption,
  catalog: LLMProviderSpec[]
): ProviderSelectOption {
  const synthetic = SYNTHETIC_DISPLAY_PROVIDERS[option.value];
  const lookupId = synthetic ? synthetic.backend : option.value;
  const spec = findSpec(catalog, lookupId);
  return {
    value: option.value,
    label: option.label,
    tier: spec?.tier ?? 'unverified',
    notesForUser: synthetic ? '' : (spec?.notes_for_user ?? ''),
    description: synthetic?.description,
  };
}

const TIER_GROUP_LABELS: Record<ProviderTier, string> = {
  native: 'Native reasoning',
  gateway: 'Gateway',
  unverified: 'Unverified',
};

const TIER_ORDER: ProviderTier[] = ['native', 'gateway', 'unverified'];

/**
 * Build tier-grouped picker options from the catalog, layered on top of the
 * curated display lists. Synthetic display providers (anthropic_proxy,
 * anthropic_direct, openai_custom, local_openai) are routed to the tier of
 * their backend provider and carry a short description sub-line.
 *
 * Returns groups in the order: native, gateway, unverified. Empty groups are
 * omitted.
 */
export function buildProviderGroups(
  catalog: LLMProviderSpec[],
  options: {
    includeLocal?: boolean;
    includeLocalOpenAISentinel?: boolean;
  } = {}
): ProviderSelectGroup[] {
  const { includeLocal = true, includeLocalOpenAISentinel = true } = options;

  const curated: ProviderOption[] = [
    ...VERIFIED_PROVIDER_OPTIONS,
    ...HOSTED_OPENAI_COMPATIBLE_PROVIDER_OPTIONS,
    ...(includeLocal
      ? [
          ...(includeLocalOpenAISentinel
            ? [{ value: 'local_openai', label: 'Local LLM (OpenAI-compatible)' }]
            : []),
          ...LOCAL_OPENAI_COMPATIBLE_PROVIDER_OPTIONS,
        ]
      : []),
  ];

  // Bucket by tier.
  const buckets: Record<ProviderTier, ProviderSelectOption[]> = {
    native: [],
    gateway: [],
    unverified: [],
  };
  for (const opt of curated) {
    const enriched = enrichOption(opt, catalog);
    buckets[enriched.tier ?? 'unverified'].push(enriched);
  }

  return TIER_ORDER.map((tier) => ({
    label: TIER_GROUP_LABELS[tier],
    tier,
    options: buckets[tier],
  })).filter((g) => g.options.length > 0);
}

// Smoke-tested end-to-end in Nymeria (matches `verified=True` in
// nymeria/config/llm_providers.py). The synthetic display values
// (anthropic_proxy, anthropic_direct, openai_custom) all resolve to a
// verified backend provider via fromSettingsDisplayProvider.
export const VERIFIED_PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'anthropic_proxy', label: 'Anthropic (Subscription)' },
  { value: 'anthropic_direct', label: 'Anthropic (Direct API)' },
  { value: 'openai', label: 'OpenAI (Direct)' },
  { value: 'openai_custom', label: 'OpenAI (Custom base URL)' },
  { value: 'openrouter', label: 'OpenRouter' },
];

export const HOSTED_OPENAI_COMPATIBLE_PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'azure-foundry', label: 'Azure AI Foundry' },
  { value: 'xai', label: 'xAI' },
  { value: 'google', label: 'Google Gemini' },
  { value: 'google-vertex', label: 'Google Vertex AI' },
  { value: 'groq', label: 'Groq' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'mistral', label: 'Mistral AI' },
  { value: 'cohere', label: 'Cohere' },
  { value: 'togetherai', label: 'Together AI' },
  { value: 'fireworks-ai', label: 'Fireworks AI' },
  { value: 'perplexity', label: 'Perplexity' },
  { value: 'cerebras', label: 'Cerebras' },
  { value: 'sambanova', label: 'SambaNova' },
  { value: 'nvidia', label: 'NVIDIA NIM' },
  { value: 'huggingface', label: 'Hugging Face' },
  { value: 'deepinfra', label: 'DeepInfra' },
  { value: 'moonshotai', label: 'Moonshot / Kimi' },
  { value: 'aihubmix', label: 'AIHubMix' },
  { value: 'alibaba', label: 'Alibaba / Qwen' },
  { value: 'alibaba-coding-plan', label: 'Alibaba Coding Plan' },
  { value: 'qwen-oauth', label: 'Qwen Portal' },
  { value: 'zai', label: 'Z.ai' },
  { value: 'zhipuai', label: 'Zhipu AI' },
  { value: 'qianfan', label: 'Baidu Qianfan' },
  { value: 'stepfun', label: 'StepFun' },
  { value: 'volcengine', label: 'Volcengine Ark' },
  { value: 'volcengine-coding-plan', label: 'Volcengine Coding Plan' },
  { value: 'byteplus', label: 'BytePlus ModelArk' },
  { value: 'vercel', label: 'Vercel AI Gateway' },
  { value: 'v0', label: 'Vercel v0' },
  { value: 'github-models', label: 'GitHub Models' },
  { value: 'github-copilot', label: 'GitHub Copilot' },
  { value: 'requesty', label: 'Requesty' },
  { value: 'poe', label: 'Poe' },
  { value: 'gmi', label: 'GMI Cloud' },
  { value: 'nous', label: 'Nous Research' },
  { value: 'tencent-tokenhub', label: 'Tencent TokenHub' },
  { value: 'novita-ai', label: 'Novita AI' },
  { value: 'siliconflow', label: 'SiliconFlow' },
  { value: 'arcee', label: 'Arcee AI' },
  { value: 'chutes', label: 'Chutes' },
  { value: 'venice', label: 'Venice AI' },
  { value: 'kilocode', label: 'Kilo Code Gateway' },
  { value: 'ollama-cloud', label: 'Ollama Cloud' },
];

export const LOCAL_OPENAI_COMPATIBLE_PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'ollama', label: 'Ollama local' },
  { value: 'lmstudio', label: 'LM Studio' },
  { value: 'llamacpp', label: 'llama.cpp server' },
  { value: 'vllm', label: 'vLLM' },
  { value: 'localai', label: 'LocalAI' },
  { value: 'litellm', label: 'LiteLLM proxy' },
  { value: 'tgi', label: 'Hugging Face TGI' },
];

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
  const normalized = baseUrl.includes('://') ? baseUrl : `http://${baseUrl}`;
  let host = '';
  try {
    host = new URL(normalized).hostname.toLowerCase();
  } catch {
    return LOCAL_HOSTS.some((localHost) => baseUrl.includes(localHost));
  }
  if (LOCAL_HOSTS.includes(host)) return true;
  if (host.endsWith('.docker.internal') || host.endsWith('.podman.internal') || host.endsWith('.lima.internal')) {
    return true;
  }
  const parts = host.split('.').map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return false;
  }
  const [first, second] = parts;
  return (
    first === 10
    || (first === 172 && second >= 16 && second <= 31)
    || (first === 192 && second === 168)
    || (first === 169 && second === 254)
    || (first === 100 && second >= 64 && second <= 127)
  );
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
  return !!provider && provider !== 'anthropic' && provider !== 'anthropic_proxy' && provider !== 'anthropic_direct';
}
